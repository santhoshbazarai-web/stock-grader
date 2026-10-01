// Server-side base URL of the FastAPI service. Inside docker-compose this is
// http://api:8000; locally it defaults to the uvicorn dev server.
export const API_URL = process.env.API_URL ?? "http://localhost:8000";
