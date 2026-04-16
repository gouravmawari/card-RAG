import os
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import PayloadSchemaType

load_dotenv()

qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY"),
)

COLLECTION = "ncert_chunks"

print("Creating payload indexes on ncert_chunks collection...")

# Index 'board' — keyword filter (exact match like board="cbse")
qdrant.create_payload_index(
    collection_name=COLLECTION,
    field_name="board",
    field_schema=PayloadSchemaType.KEYWORD,
)
print("✅ Index created: board")

# Index 'subject' — keyword filter
qdrant.create_payload_index(
    collection_name=COLLECTION,
    field_name="subject",
    field_schema=PayloadSchemaType.KEYWORD,
)
print("✅ Index created: subject")

# Index 'chapter' — keyword filter
qdrant.create_payload_index(
    collection_name=COLLECTION,
    field_name="chapter",
    field_schema=PayloadSchemaType.KEYWORD,
)
print("✅ Index created: chapter")

# Index 'page_start' — integer range filter
qdrant.create_payload_index(
    collection_name=COLLECTION,
    field_name="page_start",
    field_schema=PayloadSchemaType.INTEGER,
)
print("✅ Index created: page_start")

# Index 'page_end' — integer range filter
qdrant.create_payload_index(
    collection_name=COLLECTION,
    field_name="page_end",
    field_schema=PayloadSchemaType.INTEGER,
)
print("✅ Index created: page_end")

print("\n🎉 All indexes created. Your retrieval.py will now work.")
print("You never need to run this again unless you recreate the collection.")
