"""OWL 2 DL queries through a real HermiT/OWLAPI bridge in one local JVM.

Every uncached check owns a fresh ontology and reasoner. Only declared input
axioms participate; declarations are non-logical background in every subset.
This module translates syntax and calls standard reasoning APIs. Eligibility,
source interpretation, minimal-support enumeration and answer formatting live
outside the engine. Missing entailment is never converted into negation.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import threading
import zipfile
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlparse

from .contract import InputError, axiom, expression, identifier, obj, unique

_JAVA_LOCK = threading.RLock()
_RUNTIME = None


class EngineError(RuntimeError):
    """A bounded execution failure, distinct from a logical UNKNOWN result."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _java_error(error) -> EngineError:
    name = str(type(error))
    if "TimeOutException" in name or "TimeoutException" in name:
        return EngineError("TIMEOUT", str(error))
    if "ReasonerInterruptedException" in name:
        return EngineError("INTERRUPTED", str(error))
    if "InconsistentOntologyException" in name:
        return EngineError("INCONSISTENT_KB", str(error))
    return EngineError("REASONER_ERROR", f"{name}: {error}")


def _runtime():
    global _RUNTIME
    with _JAVA_LOCK:
        if _RUNTIME is not None:
            return _RUNTIME
        try:
            import jpype
            import owlready2
        except ImportError as error:
            raise EngineError("DEPENDENCY_MISSING", "install the world-owl extra") from error
        jar = (Path(owlready2.__file__).parent / "hermit" / "HermiT.jar").resolve()
        if not jar.is_file():
            raise EngineError("JVM_ERROR", "bundled HermiT.jar is missing")
        try:
            if not jpype.isJVMStarted():
                # JPype 1.6's macOS finder can return the JDK directory as bytes.
                # Normalize to the actual dynamic library without changing env.
                jvm_path = Path(os.fsdecode(jpype.getDefaultJVMPath()))
                if jvm_path.is_dir():
                    candidates = (
                        jvm_path / "lib/server/libjvm.dylib",
                        jvm_path / "lib/server/libjvm.so",
                        jvm_path / "bin/server/jvm.dll",
                    )
                    jvm_path = next((path for path in candidates if path.is_file()), jvm_path)
                jpype.startJVM(str(jvm_path), "-Xmx512m", classpath=[str(jar)], convertStrings=True)
            else:
                jpype.addClassPath(str(jar))
            J = jpype.JClass
            manager = J("org.semanticweb.owlapi.apibinding.OWLManager")
            reasoner_class = J("org.semanticweb.HermiT.Reasoner")
            loaded_from = reasoner_class.class_.getProtectionDomain().getCodeSource().getLocation()
            loaded_jar = Path(unquote(urlparse(str(loaded_from)).path)).resolve()
            if loaded_jar != jar:
                raise EngineError("ENGINE_MISMATCH", "JVM already loaded a different HermiT jar")
            with zipfile.ZipFile(jar) as bundle:
                manifest = bundle.read("META-INF/MANIFEST.MF").decode("utf-8")
                version = re.search(r"Implementation-Version:\s*([^\r\n\\]+)", manifest)
                properties = bundle.read(
                    "META-INF/maven/net.sourceforge.owlapi/owlapi-api/pom.properties"
                ).decode("utf-8")
                owlapi_version = re.search(r"^version=(.+)$", properties, re.MULTILINE)
            system = J("java.lang.System")
            _RUNTIME = SimpleNamespace(
                manager=manager,
                df=manager.getOWLDataFactory(),
                iri=J("org.semanticweb.owlapi.model.IRI"),
                hash_set=J("java.util.HashSet"),
                factory=J("org.semanticweb.HermiT.Reasoner$ReasonerFactory")(),
                configuration=J("org.semanticweb.owlapi.reasoner.SimpleConfiguration"),
                rdfxml_format=J("org.semanticweb.owlapi.io.RDFXMLOntologyFormat"),
                dl_profile=J("org.semanticweb.owlapi.profiles.OWL2DLProfile"),
                byte_stream=J("java.io.ByteArrayOutputStream"),
                metadata={
                    "engine": "HermiT",
                    "engineVersion": version.group(1).strip() if version else None,
                    "owlapiVersion": owlapi_version.group(1).strip() if owlapi_version else None,
                    "jarPath": str(jar),
                    "jarSha256": sha256(jar.read_bytes()).hexdigest(),
                    "owlready2": importlib.metadata.version("owlready2"),
                    "jpype1": importlib.metadata.version("jpype1"),
                    "javaVersion": str(system.getProperty("java.version")),
                    "javaVendor": str(system.getProperty("java.vendor")),
                    "javaArchitecture": str(system.getProperty("os.arch")),
                },
            )
            return _RUNTIME
        except EngineError:
            raise
        except Exception as error:
            raise EngineError("JVM_ERROR", str(error)) from error


class Engine:
    """Query a frozen set of eligible statements, without retaining closures."""

    def __init__(self, vocabulary, scope, statements, *, max_checks=8192, timeout_ms=5000):
        if type(max_checks) is not int or max_checks < 1:
            raise EngineError("INPUT_ERROR", "max_checks must be a positive integer")
        if type(timeout_ms) is not int or timeout_ms < 1:
            raise EngineError("INPUT_ERROR", "timeout_ms must be a positive integer")
        self.max_checks = max_checks
        self.timeout_ms = timeout_ms
        self.checks = 0
        self.cache_hits = 0
        self._cache = {}
        self.vocabulary = deepcopy(vocabulary)
        self.scope = {key: scope[key] for key in ("workId", "contextId")}
        try:
            obj(self.vocabulary, "classes individuals properties", "vocabulary")
            names = [unique(self.vocabulary[key]) for key in self.vocabulary]
            if sum(map(len, names)) != len(set().union(*names)):
                raise InputError("vocabulary names must not overlap")
            for value in self.scope.values():
                identifier(value)
            unique(statements, keyed=True)
            for row in statements:
                axiom(row["axiom"], self.vocabulary)
                for key, value in self.scope.items():
                    if key in row and row[key] != value:
                        raise InputError("statement lies outside the engine scope")
        except (InputError, KeyError, TypeError) as error:
            raise EngineError("INPUT_ERROR", str(error)) from error
        self._rt = _runtime()
        digest = sha256(json.dumps(self.scope, sort_keys=True).encode()).hexdigest()
        self._prefix = f"urn:catchhole:world-owl:{digest}:"
        df = self._rt.df
        self._classes = {
            name: df.getOWLClass(self._iri("class", name)) for name in self.vocabulary["classes"]
        }
        self._individuals = {
            name: df.getOWLNamedIndividual(self._iri("individual", name))
            for name in self.vocabulary["individuals"]
        }
        self._properties = {
            name: df.getOWLObjectProperty(self._iri("property", name))
            for name in self.vocabulary["properties"]
        }
        self._declarations = tuple(
            df.getOWLDeclarationAxiom(value)
            for index in (self._classes, self._individuals, self._properties)
            for value in index.values()
        )
        self._axioms = {row["id"]: self._axiom(row["axiom"]) for row in statements}
        with _JAVA_LOCK:
            manager, ontology = self._ontology(tuple(self._axioms))
            try:
                report = self._rt.dl_profile().checkOntology(ontology)
                if not report.isInProfile():
                    raise EngineError("OWL_PROFILE_ERROR", str(report))
            except EngineError:
                raise
            except Exception as error:
                raise _java_error(error) from error
            finally:
                manager.removeOntology(ontology)

    def _iri(self, kind, name):
        return self._rt.iri.create(f"{self._prefix}{kind}/{quote(name, safe='')}")

    def _set(self, values):
        result = self._rt.hash_set()
        for value in values:
            result.add(value)
        return result

    def _expression(self, value):
        df = self._rt.df
        if isinstance(value, str):
            return self._classes[value]
        operator = value["op"]
        if operator in {"and", "or"}:
            operands = self._set(self._expression(child) for child in value["args"])
            # OWLAPI stores operands in sets, while OWL 2 Boolean constructors
            # require at least two syntactic operands. Idempotence is exact.
            if operands.size() == 1:
                return operands.iterator().next()
            method = df.getOWLObjectIntersectionOf if operator == "and" else df.getOWLObjectUnionOf
            return method(operands)
        if operator == "not":
            return df.getOWLObjectComplementOf(self._expression(value["arg"]))
        if operator == "some":
            return df.getOWLObjectSomeValuesFrom(
                self._properties[value["property"]], self._expression(value["filler"])
            )
        raise EngineError("INPUT_ERROR", "unsupported class-expression operator")

    def _axiom(self, value, *, negative=False):
        df = self._rt.df
        kind = value["type"]
        if kind == "subclass":
            return df.getOWLSubClassOfAxiom(
                self._expression(value["sub"]), self._expression(value["super"])
            )
        if kind in {"equivalent", "disjoint"}:
            left, right = (self._expression(value[key]) for key in ("left", "right"))
            if left.equals(right):
                # Preserve A equivalent A (a tautology) and A disjoint A (A is
                # empty) rather than constructing an invalid singleton axiom.
                target = right if kind == "equivalent" else df.getOWLObjectComplementOf(right)
                return df.getOWLSubClassOfAxiom(left, target)
            operands = self._set((left, right))
            method = (
                df.getOWLEquivalentClassesAxiom
                if kind == "equivalent"
                else df.getOWLDisjointClassesAxiom
            )
            return method(operands)
        if kind == "classAssertion":
            class_expression = self._expression(value["class"])
            if negative:
                class_expression = df.getOWLObjectComplementOf(class_expression)
            return df.getOWLClassAssertionAxiom(
                class_expression, self._individuals[value["individual"]]
            )
        if kind == "propertyAssertion":
            method = (
                df.getOWLNegativeObjectPropertyAssertionAxiom
                if negative or value.get("negative", False)
                else df.getOWLObjectPropertyAssertionAxiom
            )
            return method(
                self._properties[value["property"]],
                self._individuals[value["subject"]],
                self._individuals[value["object"]],
            )
        raise EngineError("UNSUPPORTED_QUERY", "unsupported axiom kind")

    def _ids(self, ids):
        if isinstance(ids, (str, bytes)):
            raise EngineError("INPUT_ERROR", "statement IDs must be an iterable of identifiers")
        try:
            selected = tuple(sorted(set(ids)))
        except TypeError as error:
            raise EngineError("INPUT_ERROR", "invalid statement ID collection") from error
        if any(key not in self._axioms for key in selected):
            raise EngineError("UNKNOWN_STATEMENT", "query subset includes an unknown statement ID")
        return selected

    def _ontology(self, selected):
        manager = self._rt.manager.createOWLOntologyManager()
        ontology = manager.createOntology(
            self._set((*self._declarations, *(self._axioms[key] for key in selected)))
        )
        return manager, ontology

    def _evaluate(self, selected, action, payload=None, cache_value=None):
        key = (selected, action, cache_value)
        with _JAVA_LOCK:
            if key in self._cache:
                self.cache_hits += 1
                return self._cache[key]
            consistency_key = (selected, "consistent", None)
            if action != "consistent" and self._cache.get(consistency_key) is False:
                raise EngineError("INCONSISTENT_KB", "query subset is inconsistent")
            if self.checks >= self.max_checks:
                raise EngineError("CHECK_LIMIT", "fresh reasoner check budget exhausted")
            self.checks += 1
            manager = ontology = reasoner = None
            try:
                manager, ontology = self._ontology(selected)
                reasoner = self._rt.factory.createReasoner(
                    ontology, self._rt.configuration(self.timeout_ms)
                )
                if int(reasoner.getConfiguration().individualTaskTimeout) != self.timeout_ms:
                    raise EngineError("JVM_ERROR", "reasoner timeout configuration was not applied")
                consistent = bool(reasoner.isConsistent())
                self._cache[consistency_key] = consistent
                if action == "consistent":
                    result = consistent
                elif not consistent:
                    raise EngineError("INCONSISTENT_KB", "query subset is inconsistent")
                elif action == "entailed":
                    result = bool(reasoner.isEntailed(payload))
                else:
                    result = bool(reasoner.isSatisfiable(payload))
                self._cache[key] = result
                return result
            except EngineError:
                raise
            except Exception as error:
                raise _java_error(error) from error
            finally:
                if reasoner is not None:
                    reasoner.dispose()
                if manager is not None and ontology is not None:
                    manager.removeOntology(ontology)

    def consistent(self, ids) -> bool:
        return self._evaluate(self._ids(ids), "consistent")

    def entailed(self, ids, query, *, negative=False) -> bool:
        if type(negative) is not bool:
            raise EngineError("INPUT_ERROR", "negative must be a boolean")
        value = deepcopy(query)
        value.setdefault("id", "query")
        try:
            axiom(value, self.vocabulary, query=True)
        except (InputError, KeyError, TypeError) as error:
            raise EngineError("UNSUPPORTED_QUERY", str(error)) from error
        if value["type"] == "satisfiable":
            raise EngineError("UNSUPPORTED_QUERY", "use satisfiable() for class satisfiability")
        if value["type"] == "subclass" and negative:
            raise EngineError("UNSUPPORTED_QUERY", "negative subclass answers are not supported")
        value.pop("id")
        cache_value = json.dumps([value, negative], sort_keys=True, separators=(",", ":"))
        return self._evaluate(
            self._ids(ids), "entailed", self._axiom(value, negative=negative), cache_value
        )

    def satisfiable(self, ids, expr) -> bool:
        try:
            expression(expr, self.vocabulary)
        except (InputError, KeyError, TypeError) as error:
            raise EngineError("UNSUPPORTED_QUERY", str(error)) from error
        return self._evaluate(
            self._ids(ids),
            "satisfiable",
            self._expression(expr),
            json.dumps(expr, sort_keys=True, separators=(",", ":")),
        )

    def export_rdfxml(self, ids=None) -> str:
        """Serialize asserted selected axioms, never a materialized closure."""
        selected = self._ids(self._axioms if ids is None else ids)
        with _JAVA_LOCK:
            manager, ontology = self._ontology(selected)
            try:
                stream = self._rt.byte_stream()
                manager.saveOntology(ontology, self._rt.rdfxml_format(), stream)
                return str(stream.toString("UTF-8"))
            except Exception as error:
                raise _java_error(error) from error
            finally:
                manager.removeOntology(ontology)

    def metadata(self) -> dict:
        return {
            **self._rt.metadata,
            "scope": deepcopy(self.scope),
            "timeoutMs": self.timeout_ms,
            "maxChecks": self.max_checks,
            "checks": self.checks,
            "cacheHits": self.cache_hits,
            "freshOntologyPerCheck": True,
            "profile": "bounded input subset of OWL 2 DL",
            "profileChecked": True,
        }
