from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime, timedelta
import sqlite3
from contextlib import contextmanager
import logging
from functools import wraps
import time

# إعداد التسجيل
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# تهيئة التطبيق
app = FastAPI(
    title="نظام تسجيل المواليد",
    description="نظام لتسجيل وإدارة بيانات المواليد",
    version="1.0.0"
)

# إعداد CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# إعداد الملفات الثابتة والقوالب
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# نموذج البيانات مع التحقق
class BirthData(BaseModel):
    father_id_type: Literal["رقم الموحدة", "رقم هوية الأحوال"]
    father_id: int = Field(..., description="رقم الأب")
    mother_id_type: Literal["رقم الموحدة", "رقم هوية الأحوال"]
    mother_id: int = Field(..., description="رقم الأم")
    mother_name: str = Field(..., min_length=2, max_length=100, description="اسم الأم")
    hospital_name: str = Field(..., min_length=2, max_length=100, description="اسم المستشفى")
    birth_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="تاريخ الميلاد (YYYY-MM-DD)")

# إدارة قاعدة البيانات
class DatabaseManager:
    def __init__(self, db_name="births.db"):
        self.db_name = db_name
        self.init_db()

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS births (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                father_id INTEGER,
                mother_id INTEGER,
                mother_name TEXT,
                hospital_name TEXT,
                birth_date TEXT,
                created_at TEXT
            )
            """)
            conn.commit()

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_name)
        try:
            yield conn
        finally:
            conn.close()

# تهيئة مدير قاعدة البيانات
db_manager = DatabaseManager()

# مزخرف للتحكم في معدل الطلبات
def rate_limit(calls: int, period: int):
    def decorator(func):
        last_reset = {}
        call_count = {}
        
        @wraps(func)
        async def wrapper(*args, **kwargs):
            now = time.time()
            if func.__name__ not in last_reset:
                last_reset[func.__name__] = now
                call_count[func.__name__] = 0
            
            if now - last_reset[func.__name__] >= period:
                last_reset[func.__name__] = now
                call_count[func.__name__] = 0
            
            if call_count[func.__name__] >= calls:
                raise HTTPException(
                    status_code=429,
                    detail="تم تجاوز الحد المسموح به من الطلبات. الرجاء المحاولة لاحقاً."
                )
            
            call_count[func.__name__] += 1
            return await func(*args, **kwargs)
        return wrapper
    return decorator

@app.get("/", response_class=HTMLResponse)
@rate_limit(calls=100, period=60)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/save-data/")
@rate_limit(calls=10, period=60)
async def save_data(data: BirthData):
    try:
        # التحقق من نوع رقم الأب والأم
        if data.father_id_type not in ["رقم الموحدة", "رقم هوية الأحوال"]:
            raise HTTPException(status_code=400, detail="نوع رقم الأب غير صحيح.")
        if data.mother_id_type not in ["رقم الموحدة", "رقم هوية الأحوال"]:
            raise HTTPException(status_code=400, detail="نوع رقم الأم غير صحيح.")

        with db_manager.get_connection() as conn:
            cursor = conn.cursor()

            # التحقق من وجود البيانات مسبقاً
            cursor.execute("""
            SELECT hospital_name FROM births 
            WHERE father_id = ? AND mother_id = ?
            """, (data.father_id, data.mother_id))
            result = cursor.fetchone()

            if result:
                raise HTTPException(
                    status_code=400,
                    detail=f"تم إدخال هذه البيانات بالفعل في مستشفى {result[0]}"
                )

            # إدخال البيانات الجديدة
            created_at = datetime.now().isoformat()
            cursor.execute("""
            INSERT INTO births (father_id, mother_id, mother_name, hospital_name, birth_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (data.father_id, data.mother_id, data.mother_name, data.hospital_name, data.birth_date, created_at))
            conn.commit()

            logger.info(f"تم حفظ بيانات جديدة: {data.mother_name}")
            return {"message": "تم حفظ البيانات بنجاح"}

    except Exception as e:
        logger.error(f"خطأ في حفظ البيانات: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/delete-old-entries/")
@rate_limit(calls=5, period=3600)  # 5 calls per hour
async def delete_old_entries():
    try:
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cutoff_time = (datetime.now() - timedelta(days=30)).isoformat()
            
            cursor.execute("""
            DELETE FROM births 
            WHERE created_at < ?
            """, (cutoff_time,))
            
            deleted_count = cursor.rowcount
            conn.commit()
            
            logger.info(f"تم حذف {deleted_count} سجلات قديمة")
            return {
                "message": f"تم حذف {deleted_count} إدخالات قديمة",
                "details": {"deleted_count": deleted_count}
            }
    except Exception as e:
        logger.error(f"خطأ في حذف السجلات القديمة: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/search/")
@rate_limit(calls=20, period=60)
async def search_data(father_id: int, mother_id: int):
    try:
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM births 
            WHERE father_id = ? AND mother_id = ?
            """, (father_id, mother_id))
            result = cursor.fetchall()

            if not result:
                raise HTTPException(status_code=404, detail="لم يتم العثور على بيانات.")

            return {"data": result}

    except Exception as e:
        logger.error(f"خطأ في البحث عن البيانات: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    logger.info("بدء تشغيل التطبيق...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
