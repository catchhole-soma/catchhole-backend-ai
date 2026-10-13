"""Local GH241 acceptance adapter: real QA/runtime, scripted model only."""

import os
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from chat_stub_server import main
from app.chatbot.hybrid import HybridQaHandler
from app.retrieval.search import RetrievalSearchService
from app.chatbot.qa import ChatQaHandler
from app.chatbot.replay import ScriptedTextClient, offline_settings


async def conversation_handler(execution):
    # A follow-up must arrive with the server's conversation, never a browser-supplied history.
    if execution.context.question.startswith("그") and not execution.context.conversation:
        raise AssertionError("Follow-up conversation was not delivered")
    if "장면" in execution.context.question:
        url = make_url(os.environ["GH241_TEST_DATABASE_URL"])
        if url.host not in {"localhost", "127.0.0.1"} or url.database != "gh241_test":
            raise RuntimeError("Disposable local gh241_test database required")
        engine = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
        outputs = [
            {
                "disposition": "PROCEED",
                "rdbMode": "NONE",
                "sourceNeed": "SCENE",
                "subjects": [],
                "searchTerms": ["아리아", "반지"],
                "includeEvidence": True,
                "episodeFrom": None,
                "episodeTo": None,
            },
            {
                "coverage": "COMPLETE",
                "claims": [
                    {
                        "text": "아리아는 추적을 피하려고 반지를 강물에 던졌습니다.",
                        "evidenceRefs": ["E1"],
                    }
                ],
                "conflicts": [],
            },
        ]
        try:
            # No provider or embedding client is constructed. This fixture is keyword sufficient.
            return await HybridQaHandler(
                ScriptedTextClient(outputs),
                RetrievalSearchService(sessionmaker(engine)),
                None,
                offline_settings(),
            )(execution)
        finally:
            engine.dispose()
    outputs = [
        {
            "intent": "CURRENT",
            "subjects": [{"domain": "WORLD", "name": "화염술"}],
            "includeEvidence": False,
            "episodeFrom": None,
            "episodeTo": None,
        },
        {"outcome": "ANSWER", "rowRefs": ["R1"]},
    ]
    return await ChatQaHandler(ScriptedTextClient(outputs), offline_settings())(execution)


if __name__ == "__main__":
    main(handler_override=conversation_handler)
