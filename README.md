# Employee Attendance & Analytics API

FastAPI and MongoDB backend implementing employee attendance tracking, manual regularization, and analytics aggregation pipelines.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Local instance or MongoDB Atlas)

## Setup & Running

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   Set `MONGO_URI` and `MONGO_DB` via environment variables (or `.env` file):
   ```bash
   MONGO_URI=mongodb://127.0.0.1:27017
   MONGO_DB=attendance_db
   ```

3. **(Optional) Seed sample data:**
   ```bash
   python sample_seed.py
   ```

4. **Start the API service:**
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

5. **API Documentation:**
   Interactive Swagger UI is available at `http://127.0.0.1:8000/docs`.

## Project Structure
- `app/main.py`: Complete FastAPI application, Pydantic models, indexing, and MongoDB aggregation pipelines.
- `REVIEW.md`: Documentation of starter defects, symptoms, and corresponding fixes.
- `DECISIONS.md`: Architectural decisions regarding indexing, concurrency, ranking, and scalability.
- `requirements.txt`: Python package dependencies.
