import os
from google import genai
from dotenv import load_dotenv

# Ensure you have set your GEMINI_API_KEY environment variable
# e.g., in terminal: set GEMINI_API_KEY="your_api_key" (Windows) 
# or export GEMINI_API_KEY="your_api_key" (Mac/Linux)

def test_api():
    try:
        # Load environment variables from .env before creating the client.
        load_dotenv()

        if not os.getenv("GEMINI_API_KEY"):
            print("Failed to connect: GEMINI_API_KEY is not set. Check your .env file.")
            return

        # The SDK picks up GEMINI_API_KEY from environment variables.
        client = genai.Client()

        print("Sending ping to Gemini...")
        response = client.models.generate_content(
            model='gemini-3-flash-preview', # Use the latest standard model
            contents='Return the exact string: "API Connection Successful."'
        )
        print(f"Response: {response.text}")
    except Exception as e:
        print(f"Failed to connect: {e}")

if __name__ == "__main__":
    test_api()