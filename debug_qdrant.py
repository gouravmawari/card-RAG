import os
from dotenv import load_dotenv
from qdrant_client import QdrantClient

load_dotenv()

qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY"),
)

COLLECTION_NAME = "ncert_chunks"

try:
    info = qdrant.get_collection(COLLECTION_NAME)
    print(f"Collection: {COLLECTION_NAME}")
    print(f"Points count: {info.points_count}")

    # Get a sample of points to see what the payload looks like
    search_result = qdrant.scroll(
        collection_name=COLLECTION_NAME,
        limit=5,
        with_payload=True
    )

    print("\nSample Payloads:")
    for i, point in enumerate(search_result[0]):
        print(f"Point {i}:")
        for k, v in point.payload.items():
            print(f"  {k}: {v}")

except Exception as e:
    print(f"Error: {e}")
