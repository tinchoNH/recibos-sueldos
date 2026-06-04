import os
import re
import json
import uuid
import shutil
import smtplib
from pathlib import Path
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

import pdfplumber
from pypdf import PdfReader, PdfWriter
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Recibos de Sueldo")

# En Render los archivos temporales van a /tmp; localmente en la carpeta del proyecto
ON_RENDER = bool(os.getenv("RENDER"))
_base = Path("/tmp") if ON_RENDER else Path(".")

UPLOAD_DIR = _base / "uploads"
OUTPUT_DIR = _base / "output"
CONFIG_DIR  = _base / "config"
STATIC_DIR  = Path("static")

for d in [UPLOAD_DIR, OUTPUT_DIR, CONFIG_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

EMPLOYEES_FILE = CONFIG_DIR / "employees.json"
SMTP_FILE      = CONFIG_DIR / "smtp.json"

sessions: dict = {}


# ── Employees ──────────────────────────────────────────────────────────────

def load_employees() -> dict:
    if EMPLOYEES_FILE.exists():
        return json.loads(EMPLOYEES_FILE.read_text(encoding="utf-8"))
    return {}


def save_employees(data: dict):
    EMPLOYEES_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── SMTP ───────────────────────────────────────────────────────────────────

def load_smtp() -> dict:
    """Lee SMTP desde variables de entorno (Render) o desde archivo (local)."""
    if os.getenv("SMTP_HOST"):
        return {
            "host":      os.getenv("SMTP_HOST", ""),
            "port":      int(os.getenv("SMTP_PORT", "587")),
            "user":      os.getenv("SMTP_USER", ""),
            "password":  os.getenv("SMTP_PASSWORD", ""),
            "from_name": os.getenv("SMTP_FROM_NAME", "RRHH"),
            "use_ssl":   os.getenv("SMTP_USE_SSL", "false").lower() == "true",
        }
    if SMTP_FILE.exists():
        return json.loads(SMTP_FILE.read_text(encoding="utf-8"))
    return {}


def smtp_from_env() -> bool:
    return bool(os.getenv("SMTP_HOST"))


# ── Name extraction ────────────────────────────────────────────────────────

def extract_employee_name(text: str) -> str:
    # Formato del sistema: "APELLIDO Y NOMBRES" en una línea, nombre en la siguiente
    match = re.search(
        r"APELLIDO\s+Y\s+NOMBRES?\s*\n\s*([A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑa-záéíóúüñ\s]+?)(?:\n|C\.U\.I\.L|CUIL)",
        text, re.IGNORECASE
    )
    if match:
        name = re.sub(r"\s+", " ", match.group(1)).strip()
        if 3 < len(name) < 70:
            return name

    # Fallbacks para otros formatos
    for pattern in [
        r"(?:Apellido\s+y\s+Nombre|APELLIDO\s+Y\s+NOMBRE)[:\s]+([A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑa-záéíóúüñ\s,\.]+?)(?:\n|CUIL|LEGAJO|DNI|$)",
        r"(?:Empleado|EMPLEADO|Agente|AGENTE)[:\s]+([A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑa-záéíóúüñ\s,\.]+?)(?:\n|CUIL|LEGAJO|$)",
    ]:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(",. ")
            if 3 < len(name) < 70:
                return name
    return ""


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF")

    session_id  = str(uuid.uuid4())
    session_dir = OUTPUT_DIR / session_id
    session_dir.mkdir()

    pdf_path = UPLOAD_DIR / f"{session_id}.pdf"
    content  = await file.read()
    pdf_path.write_bytes(content)

    employees = load_employees()
    receipts  = []

    try:
        reader = PdfReader(str(pdf_path))
        total  = len(reader.pages)

        with pdfplumber.open(str(pdf_path)) as plumber_pdf:
            for i in range(total):
                page        = reader.pages[i]
                plumber_page = plumber_pdf.pages[i]

                writer = PdfWriter()
                writer.add_page(page)
                page_pdf = session_dir / f"recibo_{i + 1:03d}.pdf"
                with open(str(page_pdf), "wb") as f:
                    writer.write(f)

                text          = plumber_page.extract_text() or ""
                detected_name = extract_employee_name(text)
                email         = employees.get(detected_name, "")

                receipts.append({
                    "index":         i,
                    "page":          i + 1,
                    "filename":      f"recibo_{i + 1:03d}.pdf",
                    "detected_name": detected_name,
                    "name":          detected_name,
                    "email":         email,
                    "text_preview":  text[:500] if text else "(sin texto extraíble)",
                })

    except Exception as e:
        shutil.rmtree(session_dir, ignore_errors=True)
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Error procesando PDF: {str(e)}")

    sessions[session_id] = {
        "receipts":    receipts,
        "pdf_path":    str(pdf_path),
        "session_dir": str(session_dir),
    }

    return {"session_id": session_id, "receipts": receipts, "total": total}


@app.get("/api/preview/{session_id}/{filename}")
async def preview_pdf(session_id: str, filename: str):
    if session_id not in sessions:
        raise HTTPException(status_code=404)
    session_dir = Path(sessions[session_id]["session_dir"])
    pdf_file    = session_dir / filename
    if not pdf_file.exists() or not pdf_file.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(str(pdf_file), media_type="application/pdf")


# ── SMTP endpoints ─────────────────────────────────────────────────────────

class SmtpConfig(BaseModel):
    host:      str
    port:      int  = 587
    user:      str
    password:  str
    from_name: str  = "RRHH"
    use_ssl:   bool = False


@app.get("/api/smtp")
async def get_smtp():
    data = load_smtp()
    if data.get("password"):
        data["password"] = "••••••••"
    data["from_env"] = smtp_from_env()
    return data


@app.post("/api/smtp")
async def save_smtp_config(config: SmtpConfig):
    if smtp_from_env():
        # En Render la config viene de variables de entorno, no se guarda en archivo
        return {"ok": True, "note": "Usando variables de entorno — cambio no persistido"}
    existing = load_smtp()
    data = config.dict()
    if data["password"] == "••••••••" and existing.get("password"):
        data["password"] = existing["password"]
    SMTP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


@app.post("/api/smtp/test")
async def test_smtp():
    smtp_config = load_smtp()
    if not smtp_config.get("host"):
        raise HTTPException(status_code=400, detail="No hay configuración SMTP")
    try:
        if smtp_config.get("use_ssl"):
            server = smtplib.SMTP_SSL(smtp_config["host"], smtp_config["port"], timeout=10)
        else:
            server = smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=10)
            server.starttls()
        server.login(smtp_config["user"], smtp_config["password"])
        server.quit()
        return {"ok": True, "message": "✅ Conexión exitosa"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Employees endpoints ────────────────────────────────────────────────────

@app.get("/api/employees")
async def get_employees():
    return load_employees()


@app.post("/api/employees")
async def update_employees(data: dict):
    save_employees(data)
    return {"ok": True}


# ── Send ───────────────────────────────────────────────────────────────────

class ReceiptData(BaseModel):
    index: int
    name:  str
    email: str


class SendRequest(BaseModel):
    session_id: str
    receipts:   List[ReceiptData]
    subject:    str = "Recibo de sueldo"
    body:       str = "Estimado/a {nombre},\n\nAdjunto encontrará su recibo de sueldo.\n\nSaludos,\nRRHH"


@app.post("/api/send")
async def send_emails(req: SendRequest):
    smtp_config = load_smtp()
    if not smtp_config.get("host") or not smtp_config.get("password"):
        raise HTTPException(status_code=400, detail="Configure el servidor SMTP primero")

    session = sessions.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión expirada — volvé a cargar el PDF")

    session_dir  = Path(session["session_dir"])
    receipts_map = {r["index"]: r for r in session["receipts"]}

    missing = [r.name or f"Página {r.index + 1}" for r in req.receipts if not r.email.strip()]
    if missing:
        raise HTTPException(status_code=400, detail=f"Faltan emails: {', '.join(missing)}")

    # Guardar emails nuevos
    employees = load_employees()
    for r in req.receipts:
        if r.name.strip() and r.email.strip():
            employees[r.name.strip()] = r.email.strip()
    save_employees(employees)

    try:
        if smtp_config.get("use_ssl"):
            server = smtplib.SMTP_SSL(smtp_config["host"], smtp_config["port"], timeout=30)
        else:
            server = smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=30)
            server.starttls()
        server.login(smtp_config["user"], smtp_config["password"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error SMTP: {str(e)}")

    results = []
    for receipt in req.receipts:
        page_data = receipts_map.get(receipt.index)
        if not page_data:
            results.append({"name": receipt.name, "email": receipt.email, "ok": False, "error": "No encontrado"})
            continue

        pdf_file = session_dir / page_data["filename"]
        msg = MIMEMultipart()
        msg["From"]    = f"{smtp_config.get('from_name', 'RRHH')} <{smtp_config['user']}>"
        msg["To"]      = receipt.email.strip()
        msg["Subject"] = req.subject
        msg.attach(MIMEText(req.body.replace("{nombre}", receipt.name), "plain", "utf-8"))

        try:
            with open(str(pdf_file), "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            safe = re.sub(r"[^\w\s-]", "", receipt.name).strip() or f"pagina_{receipt.index + 1}"
            part.add_header("Content-Disposition", f'attachment; filename="Recibo_{safe}.pdf"')
            msg.attach(part)
            server.sendmail(smtp_config["user"], receipt.email.strip(), msg.as_string())
            results.append({"name": receipt.name, "email": receipt.email, "ok": True})
        except Exception as e:
            results.append({"name": receipt.name, "email": receipt.email, "ok": False, "error": str(e)})

    try:
        server.quit()
    except Exception:
        pass

    return {"results": results}


app.mount("/static", StaticFiles(directory="static"), name="static")
