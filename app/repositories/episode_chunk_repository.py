from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, literal, select
from sqlalchemy.orm import Session

from app.models.episode import Episode
from app.models.episode_chunk import EpisodeChunk


@dataclass(frozen=True)
class EpisodeChunkEmbeddingUpdate:
    """청크에 반영할 임베딩 필드만 묶은 저장 요청이다."""

    chunk_id: UUID
    embedding: list[float]
    embedding_model: str
    embedding_version: str
    embedded_at: datetime


@dataclass(frozen=True)
class EpisodeChunkSearchResult:
    """벡터 검색으로 찾은 원문 청크와 회차·유사도 정보다."""

    chunk: EpisodeChunk
    episode_no: int
    similarity: float


class EpisodeChunkRepository:
    # 청킹 결과의 DB 접근만 담당한다. commit/rollback은 Service 또는 Worker 계층에서 처리한다.
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_episode_id(self, episode_id: UUID) -> list[EpisodeChunk]:
        # LLM 입력이나 근거 조회에서 사용할 수 있도록 청크 순서를 보장해 가져온다.
        statement = (
            select(EpisodeChunk)
            .where(EpisodeChunk.episode_id == episode_id)
            .order_by(EpisodeChunk.chunk_index)
        )
        return list(self.session.scalars(statement).all())

    def save_all(self, chunks: list[EpisodeChunk]) -> list[EpisodeChunk]:
        # split 결과로 만들어진 여러 청크를 한 번에 저장 대기 상태로 올린다.
        self.session.add_all(chunks)
        return chunks

    def update_embeddings(
        self,
        embedding_updates: list[EpisodeChunkEmbeddingUpdate],
    ) -> list[EpisodeChunk]:
        """저장된 청크를 조회해 임베딩 관련 필드만 갱신한다.

        재청킹 등으로 대상 청크가 사라졌다면 일부만 반영하지 않도록 예외를
        발생시킨다. 트랜잭션 확정과 rollback은 호출한 Service가 담당한다.
        """

        if not embedding_updates:
            return []

        target_chunk_ids = [
            embedding_update.chunk_id
            for embedding_update in embedding_updates
        ]

        # 같은 청크를 한 요청에서 두 번 갱신하는 잘못된 입력을 미리 차단한다.
        if len(set(target_chunk_ids)) != len(target_chunk_ids):
            raise ValueError("Duplicate chunk IDs exist in embedding updates.")

        # 갱신 대상을 한 번에 조회하고, 청크 ID로 바로 찾을 수 있게 구성한다.
        statement = select(EpisodeChunk).where(EpisodeChunk.id.in_(target_chunk_ids))
        chunks_by_id = {
            chunk.id: chunk
            for chunk in self.session.scalars(statement).all()
        }

        # 모든 대상이 존재하는지 먼저 확인해 일부 청크만 변경되는 것을 막는다.
        missing_chunk_ids = [
            chunk_id
            for chunk_id in target_chunk_ids
            if chunk_id not in chunks_by_id
        ]
        if missing_chunk_ids:
            # 누락된 청크를 로그와 오류 메시지에서 바로 확인할 수 있도록 ID 목록을 남긴다.
            missing_ids = ", ".join(str(chunk_id) for chunk_id in missing_chunk_ids)
            raise ValueError(f"Embedding update targets do not exist: {missing_ids}")

        # 검증을 마친 요청 순서대로 ORM 객체의 임베딩 필드만 변경한다.
        # Session이 변경을 추적하므로 호출한 Service가 commit할 때 UPDATE가 실행된다.
        updated_chunks: list[EpisodeChunk] = []
        for embedding_update in embedding_updates:
            chunk = chunks_by_id[embedding_update.chunk_id]
            chunk.embedding = embedding_update.embedding
            chunk.embedding_model = embedding_update.embedding_model
            chunk.embedding_version = embedding_update.embedding_version
            chunk.embedded_at = embedding_update.embedded_at
            updated_chunks.append(chunk)

        return updated_chunks

    def search_similar_chunks(
        self,
        query_embedding: list[float],
        work_id: UUID,
        embedding_model: str,
        embedding_version: str,
        top_k: int,
        episode_no_from: int | None = None,
        episode_no_to: int | None = None,
        excluded_chunk_ids: list[UUID] | None = None,
    ) -> list[EpisodeChunkSearchResult]:
        """query embedding과 의미적으로 가까운 원문 청크를 Top-K 조회한다.

        pgvector의 cosine distance로 유사한 청크를 찾고, 호출자가 바로 사용할
        수 있도록 distance를 similarity로 변환해 반환한다. 검색 대상은 동일
        작품이면서 query와 같은 모델·버전으로 임베딩된 청크로 제한한다.
        회차 범위와 제외할 청크 ID는 전달된 경우에만 추가로 적용한다.
        """

        # 잘못된 검색 조건은 불필요한 DB 조회나 pgvector 연산 전에 차단한다.
        if not query_embedding:
            raise ValueError("Query embedding must not be empty.")
        if top_k <= 0:
            raise ValueError("Top-K must be greater than zero.")
        if (
            episode_no_from is not None
            and episode_no_to is not None
            and episode_no_from > episode_no_to
        ):
            raise ValueError("Episode number range is invalid.")

        # pgvector의 cosine distance는 값이 작을수록 가깝다. 화면과 후속 로직에서
        # 직관적으로 사용할 수 있도록 반환 값은 1 - distance인 similarity로 바꾼다.
        cosine_distance = EpisodeChunk.embedding.cosine_distance(query_embedding)
        similarity = (literal(1.0) - cosine_distance).label("similarity")

        # EpisodeChunk에는 work_id와 episode_no가 없으므로 Episode를 JOIN한다.
        # distance 자체로 정렬하고 LIMIT를 적용해야 HNSW cosine 인덱스를 활용할 수 있다.
        statement = (
            select(EpisodeChunk, Episode.episode_no, similarity)
            .join(Episode, Episode.id == EpisodeChunk.episode_id)
            .where(
                Episode.work_id == work_id,
                EpisodeChunk.embedding.is_not(None),
                # 서로 다른 모델·버전의 벡터는 같은 의미 공간이라고 보장할 수 없다.
                EpisodeChunk.embedding_model == embedding_model,
                EpisodeChunk.embedding_version == embedding_version,
            )
        )

        # 회차 범위는 양 끝을 포함하며, 현재 후보보다 미래 회차를 제외할 때도 사용한다.
        if episode_no_from is not None:
            statement = statement.where(Episode.episode_no >= episode_no_from)
        if episode_no_to is not None:
            statement = statement.where(Episode.episode_no <= episode_no_to)
        # 현재 후보가 나온 청크처럼 검색 결과에서 제외할 대상은 DB 단계에서 제거한다.
        if excluded_chunk_ids:
            statement = statement.where(EpisodeChunk.id.not_in(excluded_chunk_ids))

        statement = statement.order_by(cosine_distance).limit(top_k)
        rows = self.session.execute(statement).all()

        # SQL row를 Repository 밖에서 DB 컬럼 구조를 몰라도 되는 내부 결과 객체로 변환한다.
        return [
            EpisodeChunkSearchResult(
                chunk=chunk,
                episode_no=episode_no,
                similarity=float(row_similarity),
            )
            for chunk, episode_no, row_similarity in rows
        ]

    def delete_by_episode_id(self, episode_id: UUID) -> None:
        # 재분석 또는 재청킹 시 같은 회차의 이전 청킹 결과를 정리한다.
        statement = delete(EpisodeChunk).where(EpisodeChunk.episode_id == episode_id)
        self.session.execute(statement)
