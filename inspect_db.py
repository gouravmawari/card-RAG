import asyncio
import sys
import os
from pathlib import Path

# Add backend to sys.path
backend_dir = Path("backend").resolve()
sys.path.append(str(backend_dir))

from app.db.supabase import get_supabase

async def inspect_table(table_name: str):
    supabase = get_supabase()
    print(f"--- Inspecting Table: {table_name} ---")
    try:
        # We fetch one row to see the structure
        response = supabase.table(table_name).select("*").limit(1).execute()

        if not response.data:
            print(f"Table '{table_name}' is empty, but let's try to get column names from the response metadata if possible.")
            # If it's empty, we might not get keys from data,
            # but usually, we can try to fetch the schema via RPC or similar if available.
            # For now, let's try to see if we can get any columns at all.
            print("No data found in table to inspect columns.")
            return

        columns = response.data[0].keys()
        print("Columns found in table:")
        for col in columns:
            print(f" - {col}")

    except Exception as e:
        print(f"Error inspecting table: {e}")

if __name__ == "__main__":
    asyncio.run(inspect_table("cards"))
