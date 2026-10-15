"""Source provenance for transport; prompt projections deliberately omit IDs."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import AnalysisSourceKind, SettingBookReferenceTimeKind


def validate_reference_time(kind, episode_no):
    if kind == SettingBookReferenceTimeKind.EPISODE:
        if episode_no is None:
            raise ValueError("Episode reference time requires a positive narrative episode number.")
    elif episode_no is not None:
        raise ValueError("Only episode reference time can include a narrative episode number.")


class SettingBookReferenceTime(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    reference_time_kind: SettingBookReferenceTimeKind = Field(
        default=SettingBookReferenceTimeKind.UNSPECIFIED, alias="referenceTimeKind",
    )
    reference_episode_no: int | None = Field(default=None, alias="referenceEpisodeNo", ge=1, strict=True)

    @model_validator(mode="after")
    def validate_reference(self):
        validate_reference_time(self.reference_time_kind, self.reference_episode_no)
        return self

    def reference_prompt_data(self):
        return {"reference_time_kind": self.reference_time_kind, "reference_episode_no": self.reference_episode_no}


class WorkerAnalysisSource(SettingBookReferenceTime):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    source_kind: AnalysisSourceKind = Field(alias="sourceKind")
    episode_id: UUID | None = Field(default=None, alias="episodeId")
    episode_no: int | None = Field(default=None, alias="episodeNo", ge=1)
    setting_book_id: UUID | None = Field(default=None, alias="settingBookId")
    setting_book_name: str | None = Field(default=None, alias="settingBookName")
    revision_id: UUID | None = Field(default=None, alias="revisionId")
    revision_no: int | None = Field(default=None, alias="revisionNo", ge=1)
    content_hash: str | None = Field(default=None, alias="contentHash", pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def legacy_episode_null_reference(cls, data):
        if isinstance(data, dict) and data.get("sourceKind", data.get("source_kind")) == AnalysisSourceKind.EPISODE:
            data = dict(data)
            for key in ("referenceTimeKind", "reference_time_kind"):
                if key in data and data[key] is None:
                    data[key] = SettingBookReferenceTimeKind.UNSPECIFIED
        return data

    @model_validator(mode="after")
    def validate_source(self):
        if self.source_kind == AnalysisSourceKind.SETTING_BOOK:
            if self.episode_id is not None or self.episode_no is not None:
                raise ValueError("Setting-book sources cannot include an episode.")
            if self.setting_book_id is None or self.revision_id is None or self.revision_no is None:
                raise ValueError("Setting-book sources require immutable book revision identity.")
        elif self.reference_time_kind != SettingBookReferenceTimeKind.UNSPECIFIED or any(value is not None for value in (
            self.setting_book_id, self.setting_book_name, self.revision_id, self.revision_no,
        )):
            raise ValueError("Episode sources cannot include setting-book identity.")
        return self

    def prompt_data(self) -> dict:
        if self.source_kind == AnalysisSourceKind.SETTING_BOOK:
            return {
                "source_kind": self.source_kind,
                "setting_book_name": self.setting_book_name,
                "revision_no": self.revision_no,
                **self.reference_prompt_data(),
            }
        return {"source_kind": self.source_kind, "episode_no": self.episode_no}

    def identity(self) -> tuple:
        """A document name is descriptive; its frozen identity is revision-specific."""
        identity = (self.source_kind, self.episode_id, self.episode_no, self.setting_book_id,
                    self.revision_id, self.revision_no, self.content_hash)
        return identity + ((self.reference_time_kind, self.reference_episode_no)
                           if self.source_kind == AnalysisSourceKind.SETTING_BOOK else ())
