"""Agent Orchestration 실험 영속화용 SQLAlchemy 기반을 제공한다.

전체 파이프라인에서 실험 워크벤치 API와 PostgreSQL 사이의 연결·요청 단위 Session
경계를 담당한다. 기존 `/chat`의 psycopg 저장과 Alembic migration 실행은 각각
`agent_orchestration.app.db`와 migration 환경의 책임이다.

이 모듈은 공통 declarative Base, psycopg 3 dialect를 사용하는 engine 생성,
Session factory와 FastAPI 요청 단위 Session dependency를 제공한다.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import HTTPException, Request, status
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """실험 워크벤치 SQLAlchemy 모델이 공유하는 declarative base."""


def _sqlalchemy_database_url(database_url: str) -> str:
    """기존 PostgreSQL URL을 psycopg 3 SQLAlchemy dialect URL로 변환한다."""
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    return database_url


_POOL_SIZE = 20
_MAX_OVERFLOW = 20


def create_database_engine(database_url: str, connect_timeout_sec: int = 10) -> Engine:
    """실험 API가 공유할 SQLAlchemy engine을 생성한다.

    실험 endpoint는 모두 동기 `def`라 Starlette가 anyio 기본 thread pool(상한 40)에서
    실행한다. SQLAlchemy 기본 pool(5 + overflow 10 = 15)은 이 상한보다 작아, 동시
    요청이 15를 넘으면 남는 요청이 `pool_timeout`(기본 30초) 동안 블로킹된다.
    `pool_size`/`max_overflow`를 40으로 맞춰 pod당 최대 동시 요청 수와 커넥션 상한을
    일치시킨다.
    """
    sqlalchemy_url = _sqlalchemy_database_url(database_url)
    connect_args = (
        {"connect_timeout": connect_timeout_sec}
        if sqlalchemy_url.startswith("postgresql+psycopg://")
        else {}
    )
    return create_engine(
        sqlalchemy_url,
        pool_pre_ping=True,
        pool_size=_POOL_SIZE,
        max_overflow=_MAX_OVERFLOW,
        connect_args=connect_args,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """요청별 동기 Session을 만드는 factory를 반환한다."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db_session(request: Request) -> Iterator[Session]:
    """FastAPI app state에 등록된 factory에서 요청 단위 Session을 제공한다.

    lifespan이 `settings`를 먼저 채우고 `experiment_session_factory`를 뒤이어 채우는
    순서 때문에, startup 도중 인증은 통과했지만 factory가 아직 없는 짧은 창이 있다.
    `/chat`의 `_require_runtime()`과 동일하게 503으로 응답해 그 창을 500과 구분한다.
    """
    factory: sessionmaker[Session] | None = getattr(
        request.app.state, "experiment_session_factory", None
    )
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service is unavailable.",
        )
    with factory() as session:
        yield session
