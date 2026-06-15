# llm

LLM client and prompt orchestration live here.

Expected future files:

- `client.py`: provider-specific LLM API wrapper
- `schemas.py`: structured LLM input/output models
- `prompts/`: prompt templates for extraction and validation

The LLM should return structured JSON. Database writes should happen outside this package.
