import os
from google import genai

# Ensure you have set your GEMINI_API_KEY environment variable
# e.g., in terminal: set GEMINI_API_KEY="your_api_key" (Windows) 
# or export GEMINI_API_KEY="your_api_key" (Mac/Linux)

def test_api():
    try:
        # The new SDK automatically picks up the GEMINI_API_KEY env variable
        client = genai.Client()
        
        print("Sending ping to Gemini...")
        response = client.models.generate_content(
            model='gemini-2.5-flash', # Use the latest standard model
            contents='Return the exact string: "API Connection Successful."'
        )
        print(f"Response: {response.text}")
    except Exception as e:
        print(f"Failed to connect: {e}")

if __name__ == "__main__":
    test_api()