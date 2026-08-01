"""Agent Orchestration 실험 영속화용 SQLAlchemy 기반을 제공한다.

전체 파이프라인에서 실험 워크벤치 API와 PostgreSQL 사이의 연결·요청 단위 Session
경계를 담당한다. 기존 `/chat`의 psycopg 저장과 Alembic migration 실행은 각각
`agent_orchestration.app.db`와 migration 환경의 책임이다.

이 모듈은 공통 declarative Base, psycopg 3 dialect를 사용하는 engine 생성,
Session factory와 FastAPI 요청 단위 Session dependency를 제공한다.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
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


def create_database_engine(database_url: str, connect_timeout_sec: int = 10) -> Engine:
    """실험 API가 공유할 SQLAlchemy engine을 생성한다."""
    sqlalchemy_url = _sqlalchemy_database_url(database_url)
    connect_args = (
        {"connect_timeout": connect_timeout_sec}
        if sqlalchemy_url.startswith("postgresql+psycopg://")
        else {}
    )
    return create_engine(
        sqlalchemy_url,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """요청별 동기 Session을 만드는 factory를 반환한다."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db_session(request: Request) -> Iterator[Session]:
    """FastAPI app state에 등록된 factory에서 요청 단위 Session을 제공한다."""
    factory: sessionmaker[Session] = request.app.state.experiment_session_factory
    with factory() as session:
        yield session
