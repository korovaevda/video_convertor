import os
import uuid
import zipfile
import shutil
import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, BackgroundTasks, Request, Header, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
import redis.asyncio as redis
import aiofiles
import httpx

from config import config

app = FastAPI(title="Video Processing Service")

# Redis connection
redis_client = None

# API Key Security
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: str = Depends(api_key_header)):
    """Проверка API ключа"""
    if not config.API_KEY:
        # Если API ключ не настроен, пропускаем проверку
        return True
    
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="API ключ не предоставлен. Используйте заголовок X-API-Key"
        )
    
    if api_key != config.API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Неверный API ключ"
        )
    
    return True

@app.on_event("startup")
async def startup_event():
    global redis_client
    redis_client = await redis.from_url(
        config.REDIS_URL,
        password=config.REDIS_PASSWORD if config.REDIS_PASSWORD else None,
        db=config.REDIS_DB,
        encoding="utf-8",
        decode_responses=True
    )
    
    # Создаём необходимые директории
    os.makedirs(config.TEMP_DIR, exist_ok=True)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    
    print(f"✅ Сервис запущен")
    print(f"📁 Временная папка: {config.TEMP_DIR}")
    print(f"📁 Папка результатов: {config.OUTPUT_DIR}")
    print(f"🔑 API ключ: {'настроен' if config.API_KEY else 'не настроен (доступ открыт)'}")
    print(f"🔔 Webhook: {'настроен' if config.WEBHOOK_URL else 'не настроен'}")

@app.on_event("shutdown")
async def shutdown_event():
    if redis_client:
        await redis_client.close()

def get_redis_key(task_id: str) -> str:
    """Генерирует ключ Redis с префиксом"""
    return f"{config.REDIS_KEY_PREFIX}{task_id}"

async def save_task_status(task_id: str, status: str, **kwargs):
    """Сохраняет статус задачи в Redis"""
    task_data = {
        "task_id": task_id,
        "status": status,
        "created_at": datetime.now().isoformat(),
        **kwargs
    }
    
    key = get_redis_key(task_id)
    await redis_client.set(key, json.dumps(task_data))
    
    # Устанавливаем TTL (время жизни + 1 час на обработку)
    ttl_seconds = (config.FILE_RETENTION_HOURS + 1) * 3600
    await redis_client.expire(key, ttl_seconds)

async def get_task_status(task_id: str) -> Optional[dict]:
    """Получает статус задачи из Redis"""
    key = get_redis_key(task_id)
    data = await redis_client.get(key)
    
    if data:
        return json.loads(data)
    return None

async def send_webhook_notification(task_data: dict):
    """Отправляет уведомление на webhook"""
    if not config.WEBHOOK_URL:
        print("⚠️ Webhook URL не настроен, уведомление не отправлено")
        return
    
    try:
        print(f"🔔 Отправка webhook уведомления на {config.WEBHOOK_URL}")
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                config.WEBHOOK_URL,
                json=task_data,
                headers={"Content-Type": "application/json"}
            )
            
            if response.status_code == 200:
                print(f"✅ Webhook уведомление отправлено успешно")
            else:
                print(f"⚠️ Webhook вернул статус {response.status_code}: {response.text}")
    
    except httpx.TimeoutException:
        print(f"⚠️ Timeout при отправке webhook уведомления")
    except Exception as e:
        print(f"⚠️ Ошибка при отправке webhook уведомления: {e}")

async def process_video_task(
    task_id: str,
    zip_path: str,
    music_path: str,
    fade_duration: int,
    work_dir: str
):
    """Фоновая задача обработки видео"""
    try:
        print(f"🎬 Начало обработки задачи {task_id}")
        print(f"   ZIP: {zip_path}")
        print(f"   Музыка: {music_path}")
        print(f"   Рабочая папка: {work_dir}")
        
        await save_task_status(task_id, "processing", message="Распаковка архива...")
        
        # Проверяем существование ZIP файла
        if not os.path.exists(zip_path):
            await save_task_status(
                task_id, 
                "failed", 
                error=f"ZIP файл не найден: {zip_path}"
            )
            return
        
        print(f"✓ ZIP файл найден: {zip_path}")
        
        # Распаковываем ZIP архив
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(work_dir)
            print(f"✓ Архив распакован в: {work_dir}")
        except Exception as e:
            await save_task_status(
                task_id, 
                "failed", 
                error=f"Ошибка распаковки архива: {str(e)}"
            )
            return
        
        # Подсчитываем количество .mp4 файлов
        all_files = os.listdir(work_dir)
        print(f"📁 Файлы в рабочей папке: {all_files}")
        
        mp4_files = sorted([f for f in all_files if f.endswith('.mp4')])
        num_files = len(mp4_files)
        
        print(f"🎥 Найдено MP4 файлов: {num_files}")
        print(f"   Список: {mp4_files}")
        
        if num_files == 0:
            await save_task_status(
                task_id, 
                "failed", 
                error="В архиве не найдено .mp4 файлов"
            )
            return
        
        # Проверяем существование музыкального файла
        if not os.path.exists(music_path):
            await save_task_status(
                task_id, 
                "failed", 
                error=f"Музыкальный файл не найден: {music_path}"
            )
            return
        
        print(f"✓ Музыкальный файл найден: {music_path}")
        
        await save_task_status(
            task_id, 
            "processing", 
            message=f"Обработка {num_files} видео файлов..."
        )
        
        # Проверяем существование скрипта
        script_path = os.path.abspath(config.SCRIPT_PATH)
        if not os.path.exists(script_path):
            await save_task_status(
                task_id, 
                "failed", 
                error=f"Скрипт не найден: {script_path}"
            )
            return
        
        print(f"✓ Скрипт найден: {script_path}")
        
        # Проверяем права на выполнение
        if not os.access(script_path, os.X_OK):
            await save_task_status(
                task_id, 
                "failed", 
                error=f"Скрипт не имеет прав на выполнение: {script_path}"
            )
            return
        
        print(f"✓ Скрипт имеет права на выполнение")
        
        # Запускаем скрипт обработки
        result_path = os.path.join(work_dir, "result.mp4")
        
        # Используем абсолютный путь к музыке
        abs_music_path = os.path.abspath(music_path)
        
        print(f"🚀 Запуск скрипта:")
        print(f"   Команда: {script_path} {num_files} {abs_music_path} {fade_duration}")
        print(f"   CWD: {work_dir}")
        
        process = await asyncio.create_subprocess_exec(
            script_path,
            str(num_files),
            abs_music_path,
            str(fade_duration),
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        # Логируем вывод скрипта
        if stdout:
            stdout_text = stdout.decode()
            print(f"📝 STDOUT задачи {task_id}:")
            print(stdout_text)
        
        if stderr:
            stderr_text = stderr.decode()
            print(f"⚠️ STDERR задачи {task_id}:")
            print(stderr_text)
        
        print(f"🔚 Скрипт завершился с кодом: {process.returncode}")
        
        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Неизвестная ошибка"
            await save_task_status(
                task_id, 
                "failed", 
                error=f"Ошибка обработки (код {process.returncode}): {error_msg}"
            )
            return
        
        # Проверяем, что результат создан
        print(f"🔍 Проверка результата: {result_path}")
        
        if not os.path.exists(result_path):
            files_in_workdir = os.listdir(work_dir)
            await save_task_status(
                task_id, 
                "failed", 
                error=f"Результирующий файл не создан. Файлы в папке: {files_in_workdir}"
            )
            return
        
        print(f"✓ Результат создан: {result_path}")
        
        # Перемещаем результат в папку output
        output_path = os.path.join(config.OUTPUT_DIR, f"{task_id}.mp4")
        shutil.move(result_path, output_path)
        
        print(f"✓ Файл перемещён в: {output_path}")
        
        # Получаем размер файла
        file_size = os.path.getsize(output_path)
        
        print(f"✓ Размер файла: {file_size} байт")
        
        # Устанавливаем время удаления файла
        expires_at = datetime.now() + timedelta(hours=config.FILE_RETENTION_HOURS)
        
        # Сохраняем финальный статус
        task_data = {
            "task_id": task_id,
            "status": "success",
            "message": "Обработка завершена",
            "output_file": f"{task_id}.mp4",
            "file_size": file_size,
            "expires_at": expires_at.isoformat(),
            "completed_at": datetime.now().isoformat()
        }
        
        await save_task_status(
            task_id,
            "success",
            message="Обработка завершена",
            output_file=f"{task_id}.mp4",
            file_size=file_size,
            expires_at=expires_at.isoformat(),
            completed_at=datetime.now().isoformat()
        )
        
        print(f"✅ Задача {task_id} завершена успешно")
        
        # Отправляем webhook уведомление
        await send_webhook_notification(task_data)
        
        # Планируем удаление результата через N часов (в фоне)
        asyncio.create_task(delete_file_after_delay(output_path, task_id, config.FILE_RETENTION_HOURS))
        
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"❌ Ошибка в задаче {task_id}:")
        print(error_details)
        
        await save_task_status(
            task_id,
            "failed",
            error=str(e),
            details=error_details
        )
    finally:
        # Очищаем временную папку ПОСЛЕ завершения обработки
        print(f"🧹 Очистка временных файлов для задачи {task_id}")
        
        # Удаляем рабочую директорию
        if os.path.exists(work_dir):
            try:
                shutil.rmtree(work_dir)
                print(f"✓ Удалена рабочая папка: {work_dir}")
            except Exception as e:
                print(f"⚠️ Не удалось удалить рабочую папку {work_dir}: {e}")
        
        # Удаляем ZIP архив
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
                print(f"✓ Удалён ZIP архив: {zip_path}")
            except Exception as e:
                print(f"⚠️ Не удалось удалить ZIP архив {zip_path}: {e}")
        
        print(f"✅ Временные файлы для задачи {task_id} очищены")


async def delete_file_after_delay(file_path: str, task_id: str, hours: int):
    """Удаляет файл через указанное количество часов"""
    try:
        await asyncio.sleep(hours * 3600)
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"🗑️ Удалён файл {task_id}.mp4 (истёк срок хранения)")
    except Exception as e:
        print(f"⚠️ Ошибка при удалении файла {file_path}: {e}")

@app.post("/process-video", dependencies=[Depends(verify_api_key)])
async def process_video(
    request: Request,
    background_tasks: BackgroundTasks
):
    """
    Создаёт задачу на обработку видео
    
    Требуется заголовок: X-API-Key
    
    Принимает multipart/form-data с полями:
    - video: ZIP архив с видео файлами (001.mp4, 002.mp4, ...)
    - audio: MP3 файл фоновой музыки
    - fade_duration: Длительность затухания (опционально, по умолчанию 3)
    """
    
    print(f"📥 Получен запрос на обработку видео")
    
    try:
        # Получаем form data
        form = await request.form()
        
        print(f"📋 Полученные поля формы: {list(form.keys())}")
        
        # Получаем файлы
        video_archive = form.get("video")
        background_music = form.get("audio")
        fade_duration = form.get("fade_duration", "3")
        
        print(f"   video: {video_archive.filename if video_archive else 'None'}")
        print(f"   audio: {background_music.filename if background_music else 'None'}")
        print(f"   fade_duration: {fade_duration}")
        
        # Валидация
        if not video_archive:
            raise HTTPException(status_code=400, detail="Не загружен файл video")
        
        if not background_music:
            raise HTTPException(status_code=400, detail="Не загружен файл audio")
        
        if not video_archive.filename.endswith('.zip'):
            raise HTTPException(status_code=400, detail="Файл video должен быть ZIP архивом")
        
        if not background_music.filename.endswith('.mp3'):
            raise HTTPException(status_code=400, detail="Файл audio должен быть в формате MP3")
        
        # Конвертируем fade_duration
        try:
            fade_duration = int(fade_duration)
        except:
            fade_duration = 3
        
        if fade_duration < 0 or fade_duration > 10:
            raise HTTPException(status_code=400, detail="Длительность затухания должна быть от 0 до 10 секунд")
        
        # Генерируем уникальный ID задачи
        task_id = str(uuid.uuid4())
        
        print(f"🆔 Создана задача: {task_id}")
        
        # Создаём рабочую директорию
        work_dir = os.path.join(config.TEMP_DIR, task_id)
        os.makedirs(work_dir, exist_ok=True)
        
        # Сохраняем ZIP архив
        zip_path = os.path.join(config.TEMP_DIR, f"{task_id}.zip")
        print(f"💾 Сохранение ZIP: {zip_path}")
        
        async with aiofiles.open(zip_path, 'wb') as f:
            content = await video_archive.read()
            await f.write(content)
        
        print(f"✓ ZIP сохранён: {len(content)} байт")
        
        # Сохраняем музыку
        music_path = os.path.join(work_dir, "music.mp3")
        print(f"💾 Сохранение музыки: {music_path}")
        
        async with aiofiles.open(music_path, 'wb') as f:
            content = await background_music.read()
            await f.write(content)
        
        print(f"✓ Музыка сохранена: {len(content)} байт")
        
        # Сохраняем начальный статус
        await save_task_status(task_id, "pending", message="Задача создана")
        
        print(f"✓ Статус сохранён в Redis")
        
        # Запускаем фоновую обработку
        background_tasks.add_task(
            process_video_task,
            task_id,
            zip_path,
            music_path,
            fade_duration,
            work_dir
        )
        
        print(f"✓ Фоновая задача запущена")
        
        return JSONResponse(
            status_code=202,
            content={
                "task_id": task_id,
                "status": "pending",
                "message": "Задача создана и поставлена в очередь"
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"❌ Ошибка при создании задачи:")
        print(error_details)
        
        raise HTTPException(status_code=500, detail=f"Ошибка создания задачи: {str(e)}")

@app.get("/task-status/{task_id}", dependencies=[Depends(verify_api_key)])
async def get_task_status_endpoint(task_id: str):
    """
    Получает статус задачи по ID
    
    Требуется заголовок: X-API-Key
    
    Возможные статусы:
    - **pending**: Задача в очереди
    - **processing**: Задача обрабатывается
    - **success**: Задача выполнена успешно
    - **failed**: Задача завершилась с ошибкой
    """
    
    task_data = await get_task_status(task_id)
    
    if not task_data:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    return task_data

@app.get("/download/{task_id}", dependencies=[Depends(verify_api_key)])
async def download_result(task_id: str):
    """
    Скачивает готовый видеофайл по ID задачи
    
    Требуется заголовок: X-API-Key
    """
    
    # Проверяем статус задачи
    task_data = await get_task_status(task_id)
    
    if not task_data:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    if task_data["status"] != "success":
        raise HTTPException(
            status_code=400, 
            detail=f"Файл недоступен. Статус задачи: {task_data['status']}"
        )
    
    # Проверяем наличие файла
    output_file = os.path.join(config.OUTPUT_DIR, f"{task_id}.mp4")
    
    if not os.path.exists(output_file):
        raise HTTPException(status_code=404, detail="Файл не найден или удалён")
    
    return FileResponse(
        output_file,
        media_type="video/mp4",
        filename=f"result_{task_id}.mp4"
    )

@app.get("/health")
async def health_check():
    """Проверка работоспособности сервиса (без авторизации)"""
    try:
        await redis_client.ping()
        redis_status = "ok"
    except:
        redis_status = "error"
    
    return {
        "status": "ok",
        "redis": redis_status,
        "script_exists": os.path.exists(config.SCRIPT_PATH),
        "api_key_required": bool(config.API_KEY),
        "webhook_configured": bool(config.WEBHOOK_URL)
    }

@app.get("/", response_class=HTMLResponse)
async def root():
    """Главная страница с формой"""
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse(content="<h1>index.html not found</h1>", status_code=404)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
