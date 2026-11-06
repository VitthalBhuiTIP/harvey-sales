import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
model = os.getenv("OPENAI_MODEL")

if not api_key:
    raise ValueError("OPENAI_API_KEY is missing in .env")

if not model:
    raise ValueError("OPENAI_MODEL is missing in .env")

client = OpenAI(api_key=api_key)

print("Testing OpenAI connection...")

response = client.responses.create(
model=model,
input="""
You are a B2B sales research assistant.

Analyse this example company:
Industry: Automotive
Country: United States
Business: Manufacturing automotive components.

Return:
1. Company industry
2. Potential business opportunities
3. Potential customer requirements
4. Suggested sales approach
"""
)


# import os
# from dotenv import load_dotenv
# from anthropic import Anthropic

# load_dotenv()

# api_key = os.getenv("ANTHROPIC_API_KEY")
# model = os.getenv("CLAUDE_MODEL")

# if not api_key:
#     raise ValueError("ANTHROPIC_API_KEY is missing in .env")

# if not model:
#     raise ValueError("CLAUDE_MODEL is missing in .env")

# client = Anthropic(api_key=api_key)

# print("Testing Claude AI connection...")

# try:
#     response = client.messages.create(
#         model=model,
#         max_tokens=1500,
#         messages=[
#             {
#                 "role": "user",
#                 "content": """
# You are a B2B sales research assistant.

# Analyse this example company:

# Industry: Automotive
# Country: United States
# Business: Manufacturing automotive components.

# Return:
# 1. Company industry
# 2. Potential business opportunities
# 3. Potential customer requirements
# 4. Suggested sales approach
# """
#             }
#         ]
#     )

#     print("\nClaude AI response:")

#     for block in response.content:
#         if block.type == "text":
#             print(block.text)

#     print("\nClaude AI connection successful!")

# except Exception as e:
#     print("\nClaude API Error:", str(e))


# print("\nOpenAI response:")
# print(response.output_text)

# print("\nOpenAI connection successful!")
