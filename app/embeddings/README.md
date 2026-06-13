# embeddings

Embedding creation and vector search helpers live here.

Expected future files:

- `client.py`: embedding provider wrapper
- `targets.py`: decide which source chunks should be embedded
- `search.py`: retrieve relevant chunks for RAG

MVP can embed extracted evidence chunks first, then expand to full episode chunks if search quality requires it.
