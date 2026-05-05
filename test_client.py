from agent.groq_client import call_llm, call_llm_json

# Basic test
reply = call_llm(
    prompt="What is dbt used for? One sentence.",
    system="You are a data engineering expert."
)
print("Basic:", reply)

# JSON test
result = call_llm_json(
    prompt="Return a JSON with keys 'tool' and 'purpose' describing dbt.",
    system="You only return valid JSON. No explanation, no markdown."
)
print("JSON:", result)
print("Type:", type(result))  # should be <class 'dict'>