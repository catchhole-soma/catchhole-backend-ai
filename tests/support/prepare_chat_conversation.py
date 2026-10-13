"""Seed only a disposable localhost gh241_test database; never reads .env/provider keys."""

import argparse
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import psycopg


def encoded(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database, api = urlparse(args.database_url), urlparse(args.api_url)
    if (
        database.hostname not in {"127.0.0.1", "localhost"}
        or database.path != "/gh241_test"
        or api.hostname not in {"127.0.0.1", "localhost"}
        or api.scheme != "http"
    ):
        raise SystemExit("Disposable localhost gh241_test + local e2e API required")
    member = uuid4().int % (2**62)
    email = f"{uuid4()}@chat.local.invalid"
    with psycopg.connect(args.database_url) as db:
        db.execute(
            """INSERT INTO members(id,email,password_hash,phone_verified,email_verified,
            display_name,status,role,created_at,updated_at)
            VALUES(%s,%s,'test-only',false,true,'대화 테스트','ACTIVE','AUTHOR',now(),now())""",
            (member, email),
        )
    payload = (
        encoded(b'{"alg":"HS256"}')
        + b"."
        + encoded(
            json.dumps(
                dict(
                    iss="catchhole",
                    sub=str(member),
                    email=email,
                    roles=["AUTHOR"],
                    iat=int(time.time()),
                    exp=int(time.time()) + 7200,
                )
            ).encode()
        )
    )
    # This is the repository's public local-development secret, never a production secret.
    token = (
        payload
        + b"."
        + encoded(
            hmac.new(
                b"local-development-jwt-secret-must-be-at-least-32-bytes-long",
                payload,
                hashlib.sha256,
            ).digest()
        )
    ).decode()
    with httpx.Client(
        base_url=args.api_url, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        response = client.post(
            "/api/v1/works", json={"title": "후속 질문 연결 검증", "genre": "판타지"}
        )
        response.raise_for_status()
        work = response.json()["data"]["id"]
        response = client.post(
            f"/api/v1/works/{work}/world-settings",
            json={
                "category": "POWER_SYSTEM",
                "subjectName": "화염술",
                "settingName": "소모 마력",
                "settingValue": "35",
            },
        )
        response.raise_for_status()
        setting = response.json()["data"]
    episode, chunk = uuid4(), uuid4()
    original = "아리아는 반지의 추적 마법을 알아챘다. 추적을 피하려고 반지를 강물에 던졌다."
    digest = hashlib.sha256(original.encode()).hexdigest()
    key = f"works/{work}/episodes/3/content.txt"
    with psycopg.connect(args.database_url) as db:
        db.execute(
            """INSERT INTO episodes(id,work_id,episode_no,title,content_s3_key,content_s3_version,
            content_hash,content_updated_at,char_count,status,created_at,updated_at)
            VALUES(%s,%s,3,'반지',%s,'v1',%s,now(),%s,'UPLOADED',now(),now())""",
            (episode, work, key, digest, len(original)),
        )
        db.execute(
            """INSERT INTO retrieval_sources VALUES
            (%s,%s,%s,'v1',%s,'cl100k-800-100-boundary-v1',1,localtimestamp)""",
            (episode, work, key, digest),
        )
        db.execute(
            """INSERT INTO retrieval_chunks(id,episode_id,chunk_index,chunk_text,
            start_offset,end_offset,chunk_hash,token_count) VALUES(%s,%s,0,%s,0,%s,%s,30)""",
            (chunk, episode, original, len(original), digest),
        )
    source = args.storage_root / key
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(original)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.touch(mode=0o600, exist_ok=True)
    args.output.chmod(0o600)
    args.output.write_text(
        json.dumps(dict(token=token, workId=work, setting=setting, episodeId=str(episode)))
    )
    print(
        "Synthetic local fixture prepared. Dispose the test container/storage after verification."
    )


if __name__ == "__main__":
    main()
