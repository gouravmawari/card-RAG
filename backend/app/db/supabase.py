from supabase import create_client, Client
from app.core.config import settings

# Initialize the Supabase client once using the settings from our config module
# We use the Service Role Key to ensure we have full administrative access to the DB
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

def get_supabase() -> Client:
    """Returns the initialized Supabase client."""
    return supabase
