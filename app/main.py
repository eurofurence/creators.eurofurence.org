from fastapi import FastAPI

app = FastAPI(title="Eurofurence Creator System")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
