from django.core.management import call_command

def run_daily_mc_scrape():
    """
    Calls the 'daily_mc_scrape' management command.
    """
    try:
        call_command("daily_mc_scrape")
        print("Successfully executed daily_mc_scrape command.")
    except Exception as e:
        print(f"Error executing daily_mc_scrape: {e}")
