"""Real QA handler + scripted model; Spring, auth, queries and ledger remain real."""

from chat_stub_server import main

from app.chatbot.qa import ChatQaHandler
from app.chatbot.replay import ScriptedTextClient, offline_settings


async def qa_handler(execution):
    character = execution.context.question.startswith("아리아")
    outputs = [
        {
            "intent": "CURRENT",
            "subjects": [
                {
                    "domain": "CHARACTER" if character else "WORLD",
                    "name": "아리아" if character else "화염술",
                }
            ],
            "includeEvidence": True,
            "episodeFrom": None,
            "episodeTo": None,
        },
        {"outcome": "ANSWER", "rowRefs": ["R1"]},
    ]
    return await ChatQaHandler(ScriptedTextClient(outputs), offline_settings())(execution)


if __name__ == "__main__":
    main(handler_override=qa_handler)
