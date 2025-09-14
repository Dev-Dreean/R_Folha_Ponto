# server.py
import os, re, uuid, json, time, zipfile, asyncio, datetime
from typing import List, Dict, Any
from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
import anyio
import fitz  # PyMuPDF

# ==== Config ====
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)


STOPWORDS = {"CARGO","ENDERECO","ATIVIDADE","EMPREGADOR","CIDADE","RUA","ASSINATURA","CTPS","CNPS","CNPJ","CGC"}
NAME_PATTERNS = [
    r'LOCALIZAÇÃO:\s*\d+\s+([A-ZÀ-Ý ]{5,}?)(?=\s+(?:\d{5,}|CTPS:|MENSALISTA|CATEGORIA:|HORÁRIOS:))',
    r'EMPREGADO:\s*\d+\s+([A-ZÀ-Ý ]{5,}?)(?=\s+(?:CARGO:|LOCALIZAÇÃO:|CTPS:|CATEGORIA:))',
    r'EMPREGADO:\s*([A-ZÀ-Ý ]{5,})',
]

JOBS: Dict[str, Dict[str, Any]] = {}
WS: Dict[str, List[WebSocket]] = {}

app = FastAPI(title="Folha Ponto Web")
app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ==== Utils ====
def clean_text(txt: str) -> str:
    txt = txt.replace('-\n', ' ')
    return re.sub(r'\s+', ' ', txt).strip().upper()

def extract_name(text: str) -> str | None:
    if not text: return None
    for p in NAME_PATTERNS:
        m = re.search(p, text)
        if m: return m.group(1).strip()
    return None

def sanitize_name_tokens(name: str) -> str | None:
    if not name: return None
    filtered = re.sub(r'[^A-ZÀ-Ý ]', ' ', name)
    tokens = [t for t in filtered.split() if len(t) > 2 and t not in STOPWORDS]
    if len(tokens) < 2: return None
    return ' '.join(tokens[:6])

def sanitize_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s if s else "ARQUIVO"

def generate_zip_filename(filenames: List[str]) -> str:
    codes = set()
    for name in filenames:
        name_upper = os.path.splitext(os.path.basename(name))[0].upper()
        found_codes = re.findall(r'\b(\d{3,})\b', name_upper)
        codes.update(found_codes)
    
    if not codes:
        return "documentos_desmembrados.zip"
        
    sorted_codes = sorted(list(codes), key=int)
    return "_&_".join(sorted_codes) + ".zip"

async def emit(job_id: str, event: str, payload: dict):
    # Se não há conexões WebSocket ainda, armazena evento no buffer
    job = JOBS.get(job_id)
    if job is not None:
        if len(WS.get(job_id, [])) == 0:
            buf = job.get("buffer")
            if isinstance(buf, list):
                buf.append({"event": event, "data": payload})
                return  # não envia agora
    # Envia imediatamente para clientes conectados
    if job_id not in WS:
        return
    dead = []
    for ws in WS.get(job_id, []):
        try:
            await ws.send_text(json.dumps({"event": event, "data": payload}))
        except Exception:
            dead.append(ws)
    for d in dead:
        try:
            WS[job_id].remove(d)
        except ValueError:
            pass

def emit_from_worker(job_id: str, event: str, payload: dict):
    try: anyio.from_thread.run(emit, job_id, event, payload)
    except Exception: pass

# ==== Core (Sequencial e Estável) ====
def process_pdf_to_folder(src_pdf: str, out_dir: str, job_id: str, compress: bool, is_metric_run: bool = False):
    base = os.path.splitext(os.path.basename(src_pdf))[0]
    if not is_metric_run: os.makedirs(out_dir, exist_ok=True)
    stats = {"renamed": 0, "manual": 0, "manual_pages": [], "file": base, "pages": 0}
    with fitz.open(src_pdf) as doc:
        total = doc.page_count
        stats["pages"] = total
        emit_from_worker(job_id, "file_start", {"file": base, "pages": total})
        for i in range(total):
            page = doc.load_page(i)
            text = clean_text(page.get_text("text"))
            raw  = extract_name(text)
            final = sanitize_name_tokens(raw) if raw else None
            is_manual = not final
            if is_manual:
                final = f"MANUAL_{base}_{i+1}"
            final = sanitize_filename(final)
            if is_manual:
                stats["manual"] += 1
                stats["manual_pages"].append(f"Página {i+1} de {base}.pdf")
            else:
                stats["renamed"] += 1
            if not is_metric_run:
                out_path = os.path.join(out_dir, f"{final}.pdf")
                k=1
                while os.path.exists(out_path):
                    out_path = os.path.join(out_dir, f"{final}_{k}.pdf"); k+=1
                out_doc = fitz.open()
                out_doc.insert_pdf(doc, from_page=i, to_page=i)
                pdf_bytes = out_doc.write(garbage=4, deflate=True, clean=True) if compress else out_doc.write()
                out_doc.close()
                with open(out_path, "wb") as f: f.write(pdf_bytes)
            emit_from_worker(job_id, "page_done", {"file": base, "page": i+1, "newName": final})
    return stats

def make_zip(folder: str, zip_path: str):
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for root, _, files in os.walk(folder):
            for fn in files:
                full = os.path.join(root, fn)
                arc  = os.path.relpath(full, folder)
                zf.write(full, arcname=arc)

def process_normal_job(job_id: str):
    try:
        job = JOBS[job_id]
        base_out_dir, compress = job["out"], job["compress_mode"]
        urls, total_stats = [], {"renamed": 0, "manual": 0, "manual_pages": [], "files": []}
        root_processing_dir = os.path.join(base_out_dir, "arquivos_processados")
        os.makedirs(root_processing_dir, exist_ok=True)
        original_filenames = [os.path.basename(p) for p in job["in"]]
        for src_pdf_path in job["in"]:
            file_basename = os.path.splitext(os.path.basename(src_pdf_path))[0]
            file_specific_dir = os.path.join(root_processing_dir, file_basename)
            file_stats = process_pdf_to_folder(src_pdf_path, file_specific_dir, job_id, compress, is_metric_run=False)
            total_stats["renamed"] += file_stats["renamed"]
            total_stats["manual"] += file_stats["manual"]
            total_stats["manual_pages"].extend(file_stats["manual_pages"])
            total_stats["files"].append({
                "file": file_stats["file"],
                "pages": file_stats["pages"],
                "renamed": file_stats["renamed"],
                "manual": file_stats["manual"]
            })
        if job["in"]:
            zip_filename = generate_zip_filename(original_filenames)
            zip_path = os.path.join(base_out_dir, zip_filename)
            make_zip(root_processing_dir, zip_path)
            urls.append(f"/data/{job_id}/out/{zip_filename}")
        emit_from_worker(job_id, "finished", {"urls": urls, "summary": total_stats})
    except Exception as e:
        emit_from_worker(job_id, "error", {"message": str(e)})

def process_metric_job(job_id: str):
    try:
        job = JOBS[job_id]
        compress = job["compress_mode"]
        t0 = time.perf_counter()
        total_pages = 0
        for target_pdf in job["in"]:
            with fitz.open(target_pdf) as d: total_pages += d.page_count
            process_pdf_to_folder(target_pdf, None, job_id, compress, is_metric_run=True)
        elapsed = round(time.perf_counter() - t0, 2)
        ram = 0.0
        try:
            import psutil
            ram = round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
        except Exception: pass
        emit_from_worker(job_id, "metric", {"pages": total_pages, "time": elapsed, "ram": ram})
    except Exception as e:
        emit_from_worker(job_id, "error", {"message": str(e)})

# ==== UI e API ====
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse("""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Sistema Folha Ponto</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  .scrollbar-thin::-webkit-scrollbar{width:6px;}
  .scrollbar-thin::-webkit-scrollbar-thumb{background:#94a3b8;border-radius:3px;}
  .transition-all{transition:all 0.3s ease-in-out;}
  @keyframes card-in { from { opacity: 0; transform: scale(0.95); } to { opacity: 1; transform: scale(1); } }
  .card-animate-in { animation: card-in 0.3s ease-in-out forwards; }
  .hidden-animated { opacity: 0; transform: scale(0.95); pointer-events: none; }
  .visible-animated { opacity: 1; transform: scale(1); pointer-events: auto; }
</style>
</head>
<body class="bg-slate-100 text-slate-900 text-[15px]">
  <div class="max-w-6xl mx-auto p-4 md:p-8">
    <header class="mb-6 md:mb-10 flex items-center justify-between relative">
      <h1 class="text-3xl md:text-4xl font-semibold tracking-tight text-slate-800">Sistema Folha Ponto</h1>
      <img src="/static/logo.png" alt="Logo Plansul" class="h-12 w-auto"/>
      <div id="adminIndicator" class="hidden absolute top-full right-0 mt-2 px-3 py-1 bg-amber-100 text-amber-800 text-xs font-bold rounded-full">MODO ADMIN ATIVADO</div>
    </header>

    <section class="bg-white rounded-2xl shadow-sm ring-1 ring-black/5 p-5 md:p-8 space-y-8">
      <div class="text-center">
        <div id="drop" class="border-2 border-dashed border-slate-300 rounded-2xl p-6 md:p-8 text-center bg-sky-50/50 hover:bg-sky-100/70 transition-all">
          <div class="flex items-center justify-center gap-3 text-sky-700">
            <svg class="w-7 h-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5a4.5 4.5 0 004.5 4.5h9A4.5 4.5 0 0021 16.5m-9-12v12m0-12l-3 3m3-3l3 3"/></svg>
            <span>Arraste arquivos aqui ou</span>
            <button id="btnEscolher" class="inline-flex items-center gap-2 rounded-xl bg-sky-600 text-white px-4 py-2 cursor-pointer hover:bg-sky-700"><svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M12 3c.414 0 .75.336.75.75V15h3.19a.75.75 0 01.53 1.28l-3.94 3.94a.75.75 0 01-1.06 0l-3.94-3.94a.75.75 0 01.53-1.28H11.25V3.75c0-.414.336-.75.75-.75z"/></svg>
              <span>Escolha arquivos</span>
            </button>
          </div>
          <input id="file" type="file" accept="application/pdf" multiple class="sr-only" />
          <p id="fileHint" class="mt-3 text-slate-500 text-sm">Nenhum arquivo selecionado.</p>
        </div>
      </div>

      <div id="selectionBox" class="hidden space-y-4">
        <div class="flex items-center justify-between">
          <span id="selectionTitle" class="font-semibold">Selecionados</span>
          <button id="btnClear" class="text-sm text-slate-600 hover:text-slate-900 font-semibold">Limpar tudo</button>
        </div>
        <div id="cardGroupsContainer" class="space-y-4"></div>
      </div>

      <div id="actionsContainer" style="max-height: 0; opacity: 0; padding-top: 0; margin-top: 0; border-top-width: 0;" class="overflow-hidden transition-all duration-500">
          <div class="grid gap-4 sm:grid-cols-3 items-center border-t pt-6">
                <div class="flex flex-col gap-1 text-[11px] text-slate-600">
                    <label class="inline-flex items-center gap-2 select-none cursor-pointer group">
                        <input id="chkCompress" type="checkbox" class="accent-emerald-600 w-4 h-4 transition"/>
                        <span class="font-medium group-hover:text-slate-900">Compactar PDF</span>
                    </label>
                    <div id="compressionDetails" class="hidden rounded-lg border border-slate-200 bg-slate-50 px-2 py-1.5 leading-tight">
                        <div id="sizeLine" class="flex items-center flex-wrap gap-x-2 gap-y-1">
                            <span class="text-slate-500">Tamanho:</span>
                            <span id="rawSize" class="font-medium text-slate-700">--</span>
                            <svg class="w-3 h-3 text-slate-400" viewBox="0 0 24 24" fill="currentColor"><path d="M12 5l7 7-7 7-1.4-1.4L15.2 13H5v-2h10.2L10.6 6.4 12 5z"/></svg>
                            <span id="estSize" class="font-medium text-slate-700">--</span>
                            <span id="gainBadge" class="px-1 py-0.5 rounded text-[10px] font-semibold hidden"></span>
                        </div>
                    </div>
                </div>
                <div class="hidden sm:block"></div>
                                <div class="flex items-center justify-center sm:justify-end gap-1.5">
                            <button id="btnGo" class="btn-primary inline-flex items-center gap-2 rounded-xl bg-orange-500 px-4 py-2.5 text-white text-sm font-medium leading-none hover:bg-orange-600 shadow-sm">
                <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M5.25 5.653c0-.856.917-1.398 1.665-.962l11.113 6.347a1.125 1.125 0 010 1.924L6.915 19.31a1.125 1.125 0 01-1.665-.962V5.653z"/></svg>
                Executar
              </button>
                            <button id="btnAdmin" class="hidden btn-secondary inline-flex items-center gap-2 rounded-xl bg-slate-800 px-4 py-2.5 text-white text-sm font-medium leading-none hover:bg-slate-700 shadow-sm">
                  <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M4 4.5A2.5 2.5 0 016.5 2H17.5A2.5 2.5 0 0120 4.5v2.828a.5.5 0 01-.146.354l-2.5 2.5a.5.5 0 01-.708 0l-2.5-2.5A.5.5 0 0114 7.328V4.5a.5.5 0 00-.5-.5h-3a.5.5 0 00-.5.5v2.828a.5.5 0 01-.146.354l-2.5 2.5a.5.5 0 01-.708 0l-2.5-2.5A.5.5 0 014 7.328V4.5zM4 12.5A2.5 2.5 0 016.5 10h11a2.5 2.5 0 012.5 2.5v7A2.5 2.5 0 0117.5 22H6.5A2.5 2.5 0 014 19.5v-7zM14 15.5a1 1 0 10-2 0 1 1 0 002 0z"/></svg>
                  Executar Teste
              </button>
                            <button id="btnDebug" title="Acesso Restrito" class="p-2.5 rounded-xl text-slate-500 hover:text-slate-800 hover:bg-slate-100">
                <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.25a4.5 4.5 0 00-4.5 4.5v.75H6a3 3 0 00-3 3v6.75A2.25 2.25 0 005.25 19.5h13.5A2.25 2.25 0 0021 17.25V10.5a3 3 0 00-3-3h-1.5V6.75a4.5 4.5 0 00-4.5-4.5z"/></svg>
              </button>
            </div>
        </div>
      </div>
      
      <div id="historyBox" class="hidden border-t pt-6 mt-8 space-y-4">
          <h3 class="text-xl font-semibold text-center text-slate-700">Histórico</h3>
          <ul id="historyLinks" class="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(280px,1fr))]"></ul>
      </div>

    </section>
  </div>

  <div id="modalBackdrop" class="hidden fixed inset-0 bg-black/60 z-40 transition-opacity opacity-0"></div>
  <div id="modal" class="hidden hidden-animated transition-all fixed inset-0 p-4 sm:p-8 flex items-center justify-center z-50">
    <div class="bg-white rounded-2xl w-full h-full flex flex-col p-4 shadow-2xl">
      <div class="flex-shrink-0 flex items-center justify-between mb-3 border-b pb-3">
        <h3 id="modalTitle" class="font-semibold text-lg text-slate-800 truncate">Visualizando Arquivo</h3>
        <button id="modalClose" class="p-1 rounded-full hover:bg-slate-200 text-slate-500 hover:text-slate-800">
          <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M12 22C6.477 22 2 17.523 2 12S6.477 2 12 2s10 4.477 10 10-4.477 10-10 10zm0-11.414l-2.828-2.829-1.414 1.415L10.586 12l-2.828 2.828 1.414 1.415L12 13.414l2.828 2.829 1.414-1.415L13.414 12l2.828-2.828-1.414-1.415L12 10.586z"/></svg>
        </button>
      </div>
      <iframe id="modalFrame" class="w-full flex-grow border rounded-lg" title="Preview do PDF"></iframe>
    </div>
  </div>

    <div id="progressModal" class="hidden hidden-animated transition-all fixed inset-0 p-2 sm:p-4 flex items-center justify-center z-50">
        <div class="bg-white rounded-2xl w-full max-w-2xl max-h-[88vh] flex flex-col p-3 md:p-4 shadow-2xl">
    <div class="flex items-center justify-between pb-2 border-b mb-2 bg-gradient-to-r from-sky-50 to-white -mx-3 -mt-3 px-3 pt-2 rounded-t-2xl">
          <div class="flex items-center gap-2">
              <div class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></div>
              <h3 id="progressTitle" class="text-lg font-semibold text-slate-800 tracking-tight">Processando Arquivos...</h3>
          </div>
          <button id="progressClose" class="p-1 rounded-full hover:bg-slate-200 text-slate-500 hover:text-slate-800 shrink-0" title="Fechar">
              <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M12 22C6.477 22 2 17.523 2 12S6.477 2 12 2s10 4.477 10 10-4.477 10-10 10zm0-11.414l-2.828-2.829-1.414 1.415L10.586 12l-2.828 2.828 1.414 1.415L12 13.414l2.828 2.829 1.414-1.415L13.414 12l2.828-2.828-1.414-1.415L12 10.586z"/></svg>
          </button>
      </div>
    <div class="flex items-center gap-2 mb-2">
          <svg id="progressSpinner" class="animate-spin h-6 w-6 text-sky-600" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" fill="none" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path></svg>
          <div id="miniStats" class="hidden items-center gap-3 text-xs px-2 py-1.5 rounded-lg bg-slate-50 border border-slate-200">
              <span id="msPercent" class="font-semibold text-slate-700">0%</span>
              <span class="text-slate-500">Pág: <span id="msPages">0/0</span></span>
              <span class="text-slate-500">Tempo: <span id="msTime">00:00</span></span>
              <span class="text-slate-500">ETA: <span id="msEta">--:--</span></span>
          </div>
      </div>
        <div id="immersiveProgress" class="mb-4 hidden">
            <div class="flex flex-wrap items-center justify-between gap-x-6 gap-y-4 rounded-xl bg-slate-50/80 backdrop-blur-sm border p-4 shadow-inner">
                <div class="flex items-center gap-3">
                    <div class="relative w-16 h-16" id="circleContainer">
                         <div class="absolute inset-0 rounded-full bg-gradient-to-br from-sky-100 to-sky-200"></div>
                         <div class="absolute inset-0 rounded-full flex items-center justify-center">
                             <span id="circlePercent" class="text-sky-700 font-semibold text-base transition-transform">0%</span>
                         </div>
                         <div id="circleRing" class="absolute inset-0 rounded-full" style="background:conic-gradient(#0284c7 0deg,#e2e8f0 0deg);"></div>
                         <div class="absolute inset-1.5 rounded-full bg-white"></div>
                         <div class="absolute inset-0 rounded-full" style="mask:radial-gradient(circle 52% at 50% 50%,transparent 60%,black 61%);"></div>
                    </div>
                    <div class="flex flex-col text-sm leading-tight">
                         <span class="text-slate-500">Progresso</span>
                         <span class="font-semibold text-slate-800 text-lg"><span id="pagesDone">0</span>/<span id="pagesTotal">0</span></span>
                         <span class="text-slate-500">Arquivos: <span id="filesCount">0</span></span>
                    </div>
                </div>
                <div class="flex flex-col text-sm leading-tight">
                    <span class="text-slate-500">Tempo</span>
                    <span id="timeElapsed" class="font-semibold text-slate-800 text-lg">00:00</span>
                    <span class="text-xs text-slate-500">Vel. média: <span id="avgRate">0</span> pág/s</span>
                </div>
                <div class="flex flex-col text-sm leading-tight">
                    <span class="text-slate-500">Estimativa</span>
                    <span id="eta" class="font-semibold text-slate-800 text-lg">--:--</span>
                    <span class="text-xs text-slate-500">Últimos 10s</span>
                </div>
            </div>
        </div>
    <div id="perFileProgressContainer" class="flex-grow overflow-y-auto scrollbar-thin"></div>
      <div id="summaryContainer" class="hidden mt-3 flex-shrink-0"></div>
      <details id="logDetails" class="my-3 flex-shrink-0">
          <summary class="cursor-pointer text-sm font-semibold text-slate-600 hover:text-slate-900">Ver logs detalhados</summary>
          <div id="logContainer" class="scrollbar-thin mt-2 p-2.5 bg-slate-100 rounded-lg overflow-y-auto h-36 border text-[11px] leading-snug"></div>
      </details>
      <div id="resultContainer" class="mt-auto pt-3 border-t flex flex-col items-center gap-2"></div>
    </div>
  </div>
  
    <!-- Admin modal removido -->

<script>
// Estilos de mini-mode removidos (layout simplificado único)
const $ = (s) => document.querySelector(s);
// CORREÇÃO: Garante que todas as variáveis do modal de progresso sejam declaradas
const fileInput = $("#file"), dropZone = $("#drop"), fileHint = $("#fileHint"), selectionBox = $("#selectionBox");
const btnClear = $("#btnClear"), btnGo = $("#btnGo"), btnEscolher = $("#btnEscolher");
const selectionTitle = $("#selectionTitle"), cardGroupsContainer = $("#cardGroupsContainer"), actionsContainer = $("#actionsContainer");
const chkCompress = document.getElementById('chkCompress');
const compressionDetails = document.getElementById('compressionDetails');
const rawSizeEl = document.getElementById('rawSize');
const estSizeEl = document.getElementById('estSize');
const gainBadgeEl = document.getElementById('gainBadge');
let compressionEnabled = false;
const historyBox = $("#historyBox"), historyLinks = $("#historyLinks");
const modal = $("#modal"), modalBackdrop = $("#modalBackdrop"), modalClose = $("#modalClose"), modalTitle = $("#modalTitle"), modalFrame = $("#modalFrame");
const progressModal = $("#progressModal"), progressTitle = $("#progressTitle"), progressClose = $("#progressClose"), perFileProgressContainer = $("#perFileProgressContainer"), summaryContainer = $("#summaryContainer"), logDetails = $("#logDetails"), logContainer = $("#logContainer"), resultContainer = $("#resultContainer");
// Componentes admin removidos
const progressSpinner = $("#progressSpinner");
const miniStats = document.getElementById('miniStats');
const msPercent = document.getElementById('msPercent');
const msPages = document.getElementById('msPages');
const msTime = document.getElementById('msTime');
const msEta = document.getElementById('msEta');
const toggleCompactBtn = document.getElementById('toggleCompact');
// adminViewBtn removido
const immersiveProgress = document.getElementById('immersiveProgress');
const circleRing = document.getElementById('circleRing');
const circlePercent = document.getElementById('circlePercent');
const pagesDoneEl = document.getElementById('pagesDone');
const pagesTotalEl = document.getElementById('pagesTotal');
const filesCountEl = document.getElementById('filesCount');
const timeElapsedEl = document.getElementById('timeElapsed');
const avgRateEl = document.getElementById('avgRate');
const etaEl = document.getElementById('eta');
// spark removido

let pickedFiles = [], activeModalUrl = null, filesProgress = {};
let isAdmin = false;

const bytesToSize = b => b < 1024 ? b + " B" : (b < 1048576 ? (b / 1024).toFixed(1) + " KB" : (b / 1048576).toFixed(1) + " MB");

chkCompress?.addEventListener('change', ()=>{
    compressionEnabled = chkCompress.checked;
    updateSelectionUI();
});

function updateSelectionUI() {
    const count = pickedFiles.length;
    selectionTitle.innerHTML = `Selecionados <span class="text-slate-500 font-normal">(${count})</span>`;
    selectionBox.classList.toggle('hidden', count === 0);
    if (count === 0) {
        fileHint.classList.remove('hidden');
        actionsContainer.style.maxHeight = null;
        actionsContainer.style.opacity = 0;
        actionsContainer.style.paddingTop = 0;
        actionsContainer.style.borderTopWidth = 0;
        compressionDetails?.classList.add('hidden');
    } else {
        fileHint.classList.add('hidden');
        actionsContainer.style.maxHeight = "500px"; 
        actionsContainer.style.opacity = 1;
        actionsContainer.style.paddingTop = "1.5rem";
        actionsContainer.style.borderTopWidth = "1px";
        const totalBytes = pickedFiles.reduce((a,f)=>a+f.size,0);
        if(totalBytes>0 && compressionEnabled){
            const estimated = totalBytes*0.65;
            rawSizeEl.textContent = bytesToSize(totalBytes);
            estSizeEl.textContent = bytesToSize(estimated);
            const percentGain = 100 - Math.round((estimated/totalBytes)*100);
            if(gainBadgeEl){
                gainBadgeEl.textContent = (percentGain>0?`-${percentGain}%`:`+${Math.abs(percentGain)}%`);
                gainBadgeEl.classList.remove('hidden');
                const positive = percentGain > 3; // threshold para valer a pena
                gainBadgeEl.className = 'px-1 py-0.5 rounded text-[10px] font-semibold ' + (positive? 'bg-emerald-100 text-emerald-700':'bg-amber-100 text-amber-700');
            }
            compressionDetails.classList.remove('hidden');
        } else {
            compressionDetails?.classList.add('hidden');
        }
    }
}

function clearSelection() { 
    cardGroupsContainer.innerHTML = ""; 
    pickedFiles = []; 
    fileInput.value = ""; 
    updateSelectionUI(); 
}

// Ajuste responsivo do modal de progresso
function adjustProgressModal(){
    const modalEl = progressModal;
    if(modalEl.classList.contains('hidden')) return; // só ajusta quando visível
    const header = progressModal.querySelector('div > div > .flex.items-center.justify-between');
    const circleWrapper = document.getElementById('immersiveProgress');
    const summary = summaryContainer;
    const logBox = logContainer.parentElement; // details
    const resultBox = resultContainer;
    const available = progressModal.clientHeight - 40; // padding margem
    let used = 0;
    [header, circleWrapper, summary, logBox, resultBox].forEach(el=>{ if(el && !el.classList.contains('hidden')) used += el.offsetHeight; });
    // área de listas de arquivos
    const list = perFileProgressContainer;
    const extra = 80; // margem de segurança
    const target = available - (used + extra);
    if(target > 120){
        list.style.maxHeight = target + 'px';
    } else {
        list.style.maxHeight = '200px';
    }
}

window.addEventListener('resize', ()=>{
    requestAnimationFrame(adjustProgressModal);
});

// Observer para quando summary abre ou logs mudam
const progressMutationObserver = new MutationObserver(()=>{
    requestAnimationFrame(adjustProgressModal);
});
progressMutationObserver.observe(document.documentElement,{subtree:true, attributes:true, attributeFilter:['class','open']});

function toggleModal(modalElement, show) {
    if (show) {
        modalBackdrop.classList.remove('hidden');
        modalElement.classList.remove('hidden');
        setTimeout(() => {
            modalBackdrop.classList.remove('opacity-0');
            modalElement.classList.remove('hidden-animated');
            modalElement.classList.add('visible-animated');
        }, 10);
    } else {
        modalBackdrop.classList.add('opacity-0');
        modalElement.classList.remove('visible-animated');
        modalElement.classList.add('hidden-animated');
        setTimeout(() => {
            modalElement.classList.add('hidden');
            if (modal.classList.contains('hidden') && progressModal.classList.contains('hidden') && adminModal.classList.contains('hidden')) {
                modalBackdrop.classList.add('hidden');
            }
        }, 300);
    }
}

function openModal(file) { 
    activeModalUrl = URL.createObjectURL(file); 
    modalTitle.textContent = file.name; 
    modalFrame.src = activeModalUrl; 
    toggleModal(modal, true);
}

function closeModal() { 
    if (activeModalUrl) { URL.revokeObjectURL(activeModalUrl); activeModalUrl = null; } 
    modalFrame.src = 'about:blank'; 
    toggleModal(modal, false);
}

const getFileGroup = (filename) => {
    const name = filename.replace(/[\\d\\W_]+/g, ' ').trim().split(' ')[0];
    return name.charAt(0).toUpperCase() + name.slice(1).toLowerCase() || "Outros";
}

function renderCards() {
    cardGroupsContainer.innerHTML = '';
    const groups = pickedFiles.reduce((acc, file) => {
        const groupName = getFileGroup(file.name);
        if (!acc[groupName]) acc[groupName] = [];
        acc[groupName].push(file);
        return acc;
    }, {});

    Object.keys(groups).sort().forEach(groupName => {
        const groupWrapper = document.createElement('div');
        groupWrapper.className = 'space-y-3 not-last:border-b not-last:pb-4';
        
        groupWrapper.innerHTML = `
            <div class="flex items-center justify-between">
                <span class="font-semibold text-slate-700">${groupName} <span class="text-slate-500 font-normal">(${groups[groupName].length})</span></span>
            </div>
            <div class="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(240px,1fr))]"></div>`;
        const cardsGrid = groupWrapper.querySelector('.grid');
        
        groups[groupName].forEach(file => {
            let displayName = file.name;
            const codeMatch = file.name.match(/\b(\d{3,})\b/);
            if (codeMatch) {
                const extension = file.name.split('.').pop();
                displayName = `${codeMatch[0]}.${extension}`;
            }

            const card = document.createElement("div");
            card.className = "rounded-xl border bg-slate-100/80 p-3 flex items-center justify-between gap-3 card-animate-in";
            card.innerHTML = `
                <div class="flex items-center gap-3 min-w-0">
                    <span class="flex-shrink-0 inline-flex items-center justify-center w-8 h-8 rounded-lg bg-red-600 text-white font-semibold text-sm">PDF</span>
                    <div class="min-w-0">
                        <div class="font-medium text-slate-800 truncate" title="${file.name}">${displayName}</div>
                        <div class="text-xs text-slate-500">${bytesToSize(file.size)}</div>
                    </div>
                </div>
                <div class="flex-shrink-0 flex items-center gap-1">
                    <button data-preview title="Visualizar" class="p-2 rounded-lg hover:bg-slate-200 text-slate-600"><svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z"/></svg></button>
                    <button data-remove title="Remover" class="p-2 rounded-lg hover:bg-red-100 text-red-600"><svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M7 11v2h10v-2H7zm5-9C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z"/></svg></button>
                </div>`;
            card.querySelector("[data-preview]").onclick = () => openModal(file);
            card.querySelector("[data-remove]").onclick = () => { 
                card.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
                card.style.opacity = 0;
                card.style.transform = 'scale(0.9)';
                setTimeout(() => { const idx = pickedFiles.indexOf(file); if (idx > -1) { pickedFiles.splice(idx, 1); } renderCards(); updateSelectionUI(); }, 300);
            };
            cardsGrid.appendChild(card);
        });
        cardGroupsContainer.appendChild(groupWrapper);
    });
}

function handleFileSelection(fileList) {
    const newFiles = Array.from(fileList);
    clearSelection();
    pickedFiles = newFiles.filter(f => f.name.toLowerCase().endsWith('.pdf'));
    if (pickedFiles.length === 0) { fileHint.textContent = "⚠️ Nenhum arquivo PDF válido foi selecionado."; updateSelectionUI(); return; }
    renderCards();
    updateSelectionUI();
}

// Event Listeners
["dragenter", "dragover"].forEach(ev=> { dropZone.addEventListener(ev, e=>{ e.preventDefault(); dropZone.classList.add("bg-sky-100"); }); });
["dragleave", "drop"].forEach(ev=> { dropZone.addEventListener(ev, e=>{ e.preventDefault(); dropZone.classList.remove("bg-sky-100"); }); });
dropZone.addEventListener("drop", e => { if (e.dataTransfer?.files?.length) handleFileSelection(e.dataTransfer.files); });
btnEscolher.addEventListener("click", () => { fileInput.click(); });
fileInput.addEventListener("change", () => { if (fileInput.files.length > 0) handleFileSelection(fileInput.files); });
btnClear.onclick = clearSelection;
btnGo.onclick = () => { if (pickedFiles.length === 0) { return; } runJob(pickedFiles, false); };
modalClose.onclick = () => toggleModal(modal, false);
progressClose.onclick = () => toggleModal(progressModal, false);
modalBackdrop.onclick = () => {
    toggleModal(modal, false);
    toggleModal(progressModal, false);
};
// Handlers admin removidos

async function runJob(files, metricOnly) {
    console.log("--- INICIANDO NOVO TRABALHO ---");
    perFileProgressContainer.innerHTML = "";
    summaryContainer.innerHTML = "";
    summaryContainer.classList.add("hidden");
    logDetails.open = false;
    logDetails.querySelector('summary').innerHTML = "Ver logs detalhados";
    // Exibir logs só para admin
    logDetails.style.display = isAdmin ? 'block' : 'none';
    progressTitle.textContent = metricOnly ? "Testando Desempenho..." : "Processando Arquivos...";
    progressSpinner.classList.remove('hidden');
    if(!isAdmin){ miniStats?.classList.remove('hidden'); }
    filesProgress = {};
    toggleModal(progressModal, true);
    immersiveProgress.classList.add('hidden');
    // esconder botão toggleCompact se não admin
    const tcBtn = document.getElementById('toggleCompact');
    if(tcBtn){ tcBtn.style.display = isAdmin ? '' : 'none'; }
    circlePercent.textContent = '0%';
    circleRing.style.background = 'conic-gradient(#0284c7 0deg,#e2e8f0 0deg)';
    pagesDoneEl.textContent = '0';
    pagesTotalEl.textContent = '0';
    filesCountEl.textContent = '0';
    timeElapsedEl.textContent = '00:00';
    avgRateEl.textContent = '0';
    etaEl.textContent = '--:--';
    let globalTotalPages = 0; let globalDonePages = 0; let t0 = performance.now();
    let tickInterval = null;
    // spark removido
    function drawSpark(){}
    let lastPercentVal = -1;
    function updateVisual(){
        const now = performance.now();
        const elapsed = (now - t0)/1000;
        const mm = Math.floor(elapsed/60).toString().padStart(2,'0');
        const ss = Math.floor(elapsed%60).toString().padStart(2,'0');
        timeElapsedEl.textContent = `${mm}:${ss}`;
        const percent = globalTotalPages? Math.floor((globalDonePages/globalTotalPages)*100):0;
        if(percent !== lastPercentVal){
            circlePercent.textContent = percent + '%';
            circlePercent.style.transform='scale(1.15)';
            setTimeout(()=>{ circlePercent.style.transform='scale(1)'; },160);
            lastPercentVal = percent;
        }
        const deg = (percent/100)*360;
        circleRing.style.background = `conic-gradient(#0284c7 ${deg}deg,#e2e8f0 ${deg}deg)`;
        // média
        const avgRate = elapsed>0? (globalDonePages/elapsed):0;
        avgRateEl.textContent = avgRate.toFixed(1);
        if(globalTotalPages && globalDonePages>0 && avgRate>0){
            const remaining = (globalTotalPages - globalDonePages)/avgRate;
            const emm = Math.floor(remaining/60).toString().padStart(2,'0');
            const ess = Math.floor(remaining%60).toString().padStart(2,'0');
            etaEl.textContent = `${emm}:${ess}`;
        }
        // mini stats (se mini-mode)
        if(!isAdmin && miniStats){
            msPercent.textContent = percent + '%';
            msPages.textContent = `${globalDonePages}/${globalTotalPages}`;
            msTime.textContent = `${mm}:${ss}`;
            msEta.textContent = etaEl.textContent;
        }
    }

    // Layout: mini-mode se não admin
    const modalRoot = document.getElementById('progressModal');
    modalRoot.classList.toggle('mini-mode', !isAdmin);
    if(!isAdmin){
        // ocultar painel imersivo para versão super minimalista (fica só miniStats + cards)
        immersiveProgress.classList.add('hidden');
        miniStats?.classList.remove('hidden');
    } else {
        immersiveProgress.classList.add('hidden'); // inicia oculto até init
        miniStats?.classList.add('hidden');
    }
    // grade varia conforme perfil
    perFileProgressContainer.className = isAdmin 
        ? 'flex-grow overflow-y-auto scrollbar-thin grid gap-2.5 grid-cols-[repeat(auto-fill,minmax(180px,1fr))]'
        : 'flex-grow overflow-y-auto scrollbar-thin grid gap-2 grid-cols-[repeat(auto-fill,minmax(120px,1fr))]';
        files.forEach(file => {
                const fileBasename = file.name.replace(/\.pdf$/i, '').replace(/[^A-Za-z0-9]+/g, '_');
                const card = document.createElement('div');
                card.id = `progress-${fileBasename}`;
            // MODIFICADO: Classes do card para melhor espaçamento e legibilidade
                        card.className = isAdmin
                            ? 'fileCard rounded-md border border-slate-200 bg-white p-2.5 flex flex-col gap-1 text-[11px] status-pending opacity-70'
                            : 'fileCard rounded border border-slate-200 bg-white p-2 flex flex-col gap-0.5 text-[10px] status-pending opacity-70';
            card.innerHTML = `
                <div class="flex items-center gap-2">
                    <span class="statusIcon flex-shrink-0 inline-flex items-center justify-center w-5 h-5 rounded-full bg-yellow-400 text-white text-[10px] font-bold">!</span>
                    <span class="font-medium text-slate-700 truncate" title="${file.name}">${file.name}</span>
                </div>
                <div class="h-1.5 bg-slate-200 rounded-full overflow-hidden"><div class="bar h-full bg-sky-500 w-0 transition-all duration-300"></div></div>
                <span class="count font-mono text-slate-500 text-right">Aguardando...</span>
            `;
            perFileProgressContainer.appendChild(card);
        });

    const fd = new FormData();
    files.forEach(f => fd.append("files", f));
    fd.append("compress_mode", compressionEnabled ? "true" : "false");
    fd.append("metric_only", metricOnly ? "true" : "false");

    const res = await fetch("/api/process", { method: "POST", body: fd });
    const { job_id } = await res.json();
    console.log(`[PEGA-BUG] Job ID recebido: ${job_id}`);
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/${job_id}`);

    ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.event !== 'hello') console.log("[PEGA-BUG] Mensagem recebida:", msg);
        
    // Sanitização UNIFICADA: mesma regra usada na criação (colapsa blocos não alfanuméricos em underscore)
    const sanitizedFile = msg.data.file ? msg.data.file.replace(/[^A-Za-z0-9]+/g, '_') : '';

        switch (msg.event) {
            case "init": {
                const files = (msg.data && Array.isArray(msg.data.files)) ? msg.data.files : [];
                globalTotalPages = 0; globalDonePages = 0;
                if (files.length) {
                    immersiveProgress.classList.remove('hidden');
                    filesCountEl.textContent = files.length;
                    t0 = performance.now();
                    if (tickInterval) clearInterval(tickInterval);
                    tickInterval = setInterval(()=>{ updateVisual(); }, 500);
                }
                files.forEach(f => {
                    globalTotalPages += f.pages;
                    const sid = f.id;
                    let el = document.getElementById(`progress-${sid}`);
                    if (!el) {
                                                el = document.createElement('div');
                                                el.id = `progress-${sid}`;
                                                el.className = 'fileCard rounded-lg border bg-white shadow-sm p-2.5 flex flex-col gap-1.5 text-xs status-processing';
                                                el.innerHTML = `
                                                    <div class="flex items-center gap-2">
                                                        <span class="statusIcon inline-flex items-center justify-center w-5 h-5 rounded-full bg-sky-500 text-white text-[11px]">⟳</span>
                                                        <span class="font-medium text-slate-700 truncate" title="${f.file}.pdf">${f.file}.pdf</span>
                                                    </div>
                                                    <div class="h-1.5 bg-slate-200 rounded-full overflow-hidden"><div class="bar h-full bg-sky-500 w-0 transition-all duration-300"></div></div>
                                                    <span class="count font-mono text-slate-500 text-right">0/${f.pages}</span>
                                                `;
                                                perFileProgressContainer.appendChild(el);
                    } else {
                        const c = el.querySelector('.count');
                        if (c) c.textContent = `0/${f.pages}`;
                    }
                    filesProgress[f.file] = { done: 0, total: f.pages };
                });
                break; }
            case "file_start": {
                const el = document.getElementById(`progress-${sanitizedFile}`);
                if (el) {
                    el.querySelector('.count').textContent = `0/${msg.data.pages}`;
                    el.classList.remove('bg-yellow-50','status-pending');
                    el.classList.add('bg-sky-50','status-processing');
                    const icon = el.querySelector('.statusIcon');
                    if(icon){
                        icon.textContent = '⟳';
                        icon.className = 'statusIcon inline-flex items-center justify-center w-5 h-5 rounded-full bg-sky-500 text-white text-[11px]';
                    }
                }
                if (!filesProgress[msg.data.file]) {
                    // Caso file_start venha antes de init (fallback)
                    filesProgress[msg.data.file] = { done: 0, total: msg.data.pages };
                    globalTotalPages += msg.data.pages;
                    if (immersiveProgress.classList.contains('hidden')) {
                        immersiveProgress.classList.remove('hidden');
                        t0 = performance.now();
                        if (tickInterval) clearInterval(tickInterval);
                        tickInterval = setInterval(()=>{ updateVisual(); }, 500);
                    }
                }
                break; }
            case "page_done": {
                const fp = filesProgress[msg.data.file];
                if(!fp) return;
                fp.done++;
                globalDonePages++;
                let el = document.getElementById(`progress-${sanitizedFile}`);
                if(!el){
                    // tentativa de fallback: procura elementos cujo id começa com progress- e contém a base simplificada
                    const candidates = Array.from(perFileProgressContainer.querySelectorAll('[id^="progress-"]'));
                    const simple = sanitizedFile.toLowerCase();
                    el = candidates.find(c => c.id.toLowerCase() === `progress-${simple}`) ||
                         candidates.find(c => c.id.toLowerCase().startsWith(`progress-${simple}`)) ||
                         candidates.find(c => c.id.toLowerCase().includes(simple));
                    if(el) console.log('[DEBUG-FALLBACK] Match alternativo para', sanitizedFile, '->', el.id);
                }
                if (el){
                    el.querySelector('.count').textContent = `${fp.done}/${fp.total}`;
                    // completed?
                    if(fp.done === fp.total){
                        el.classList.remove('bg-sky-50','status-processing');
                        el.classList.add('bg-emerald-50','status-done');
                        const icon = el.querySelector('.statusIcon');
                        if(icon){
                            icon.textContent = '✓';
                            icon.className = 'statusIcon inline-flex items-center justify-center w-5 h-5 rounded-full bg-emerald-500 text-white text-[11px]';
                        }
                    }
                }
                pagesDoneEl.textContent = globalDonePages;
                pagesTotalEl.textContent = globalTotalPages;
                // registrar amostra para sparkline (últimos ~10s)
                const nowT = (performance.now()-t0)/1000;
                updateVisual();
                if (!metricOnly) {
                    const logEntry = document.createElement('p');
                    logEntry.className = "text-sm text-slate-600 border-b border-slate-200 pb-1 mb-1 font-mono";
                    logEntry.innerHTML = `Pág. <b>${msg.data.page}</b> de <i>${msg.data.file}.pdf</i>  <span class=\"text-slate-400\"> ➤ </span> <span class=\"font-medium text-emerald-700\">${msg.data.newName}.pdf</span>`;
                    logContainer.appendChild(logEntry);
                    logContainer.scrollTop = logContainer.scrollHeight;
                }
                break; }
            case "finished":
                progressSpinner.classList.add('hidden');
                progressTitle.textContent = "Processamento Concluído!";
                if (tickInterval) { clearInterval(tickInterval); tickInterval = null; updateVisual(); }
                circleRing.style.background = 'conic-gradient(#059669 360deg,#e2e8f0 360deg)';
                circlePercent.classList.add('text-emerald-600');
                if(!isAdmin){ msPercent.classList.add('text-emerald-600','font-bold'); }
                                const summary = msg.data.summary;
                                const totalPagesGlobal = summary.files ? summary.files.reduce((a,f)=>a+f.pages,0) : 0;
                                let html = `<div class='mb-2 text-xs text-slate-500 font-medium'>Resumo</div>`;
                                html += `<div class='grid gap-3 text-xs md:text-sm' style='grid-template-columns:repeat(auto-fill,minmax(170px,1fr));'>`;
                                html += `<div class='p-3 rounded-lg border bg-sky-50'>
                                                     <p class='font-semibold text-sky-700 mb-1'>Global</p>
                                                     <p><b>${summary.renamed}</b> renomeadas</p>
                                                     <p><b class='${summary.manual>0?'text-amber-700':''}'>${summary.manual}</b> manual</p>
                                                     <p><b>${totalPagesGlobal}</b> páginas</p>
                                                 </div>`;
                                (summary.files||[]).forEach(f=>{
                                        html += `<div class='p-3 rounded-lg border bg-white'>
                                                             <p class='font-semibold text-slate-700 mb-1 truncate' title='${f.file}.pdf'>${f.file}</p>
                                                             <p><span class='text-sky-600 font-medium'>${f.renamed}</span> renomeadas</p>
                                                             <p><span class='${f.manual>0?'text-amber-600 font-medium':'text-slate-500'}'>${f.manual}</span> manual</p>
                                                             <p class='text-xs text-slate-500'>${f.pages} pág.</p>
                                                         </div>`;
                                });
                                html += '</div>';
                                summaryContainer.innerHTML = html;
                if (summary.manual > 0) { logDetails.querySelector('summary').innerHTML = `Ver logs detalhados <span class="font-semibold text-amber-700">(${summary.manual} para revisar)</span>`; }
                summaryContainer.classList.remove("hidden");
                resultContainer.innerHTML = "";
                historyBox.classList.remove("hidden");
                for (const url of msg.data.urls) {
                    const filename = url.split("/").pop();
                    const modalBtn = document.createElement("a");
                    modalBtn.className = "inline-flex items-center justify-center gap-2 rounded-xl text-white px-4 py-2 text-base font-medium bg-emerald-600 hover:bg-emerald-700 w-full";
                    modalBtn.href = url;
                    modalBtn.innerHTML = `<svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M12 15a1 1 0 01-.7-.29l-4-4a1 1 0 111.4-1.42L12 12.59l3.3-3.3a1 1 0 111.4 1.42l-4 4a1 1 0 01-.7.29zM12 3a9 9 0 109 9 9 9 0 00-9-9zm0 16a7 7 0 117-7 7 7 0 01-7 7z"/></svg><span>Baixar ${filename}</span>`;
                    modalBtn.onclick = () => toggleModal(progressModal, false);
                    resultContainer.appendChild(modalBtn);
                    const historyLi = document.createElement("li");
                    historyLi.className = "flex items-center justify-between gap-3 rounded-xl border bg-slate-100 px-3 py-2";
                    historyLi.innerHTML = `<div class="flex items-center gap-2 min-w-0"><svg class="w-6 h-6" text-slate-500" viewBox="0 0 24 24" fill="currentColor"><path d="M5.828 20a1 1 0 01-.707-.293l-2.828-2.828a1 1 0 111.414-1.414l2.828 2.828a1 1 0 01-.707 1.707zM16 20a1 1 0 01-.7-1.71l2.83-2.83a1 1 0 011.41 1.41l-2.83 2.83a1 1 0 01-.7.3zM9 16a1 1 0 01-1-1V5a1 1 0 012 0v10a1 1 0 01-1 1zm6 0a1 1 0 01-1-1V5a1 1 0 012 0v10a1 1 0 01-1 1zm-4-3a1 1 0 010-2h2a1 1 0 010 2h-2zm-4 3a1 1 0 01-1-1V5a1 1 0 012 0v10a1 1 0 01-1 1zm12 0a1 1 0 01-1-1V5a1 1 0 012 0v10a1 1 0 01-1 1z"/></svg><span class="truncate text-slate-800">${filename}</span></div><a class="inline-flex items-center justify-center gap-2 rounded-xl text-white px-4 py-2 text-base font-medium bg-slate-700 hover:bg-slate-800 text-sm !px-3 !py-2" href="${url}">Baixar</a>`;
                    historyLinks.prepend(historyLi);
                }
                ws.close();
                clearSelection();
                break;
            case "metric":
                progressSpinner.classList.add('hidden');
                progressTitle.textContent = "Métrica de Desempenho";
                if (tickInterval) { clearInterval(tickInterval); tickInterval = null; updateVisual(); }
                const ramUsage = msg.data.ram;
                const ramLimit = 512;
                const ramColor = ramUsage > ramLimit ? 'text-red-600 font-bold' : 'text-emerald-600 font-bold';
                const ramMessage = ramUsage > ramLimit ? `(Acima do limite de ${ramLimit}MB)` : `(Dentro do limite de ${ramLimit}MB)`;
                summaryContainer.innerHTML = `<div class="p-4 bg-slate-100 rounded-lg text-center space-y-2"><p class="text-lg">Páginas Processadas: <span class="font-semibold">${msg.data.pages}</span></p><p class="text-lg">Tempo Total: <span class="font-semibold">${msg.data.time} segundos</span></p><div><p class="text-lg">Pico de RAM: <span class="${ramColor}">${ramUsage} MB</span></p><p class="text-sm text-slate-500">${ramMessage}</p></div></div>`;
                summaryContainer.classList.remove("hidden");
                logDetails.style.display = 'none';
                const closeBtnMetric = document.createElement("button");
                closeBtnMetric.className = "mt-4 w-full text-slate-600 hover:text-slate-900 py-2";
                closeBtnMetric.textContent = "Fechar";
                closeBtnMetric.onclick = () => toggleModal(progressModal, false);
                resultContainer.appendChild(closeBtnMetric); 
                ws.close();
                break;
            case "error":
                progressSpinner.classList.add('hidden');
                progressTitle.textContent = "Erro no Processamento";
                if (tickInterval) { clearInterval(tickInterval); tickInterval = null; updateVisual(); }
                circleRing.style.background = 'conic-gradient(#dc2626 360deg,#e2e8f0 360deg)';
                circlePercent.classList.add('text-red-600');
                summaryContainer.innerHTML = `<p class="text-red-600 p-4 bg-red-50 rounded-lg">${msg.data.message}</p>`;
                summaryContainer.classList.remove("hidden");
                ws.close();
                break;
            case "hello":
                // já recebido no início: pode conter total_pages
                if (typeof msg.data.total_pages === 'number') {
                    // Só mostra depois do primeiro file_start para evitar 0/0
                }
                break;
        }
    };
}
</script>
</body>
</html>""")

# ==== API ====
@app.post("/api/process")
async def process_endpoint(
    files: List[UploadFile] = File(...),
    compress_mode: str = Form("false"),
    metric_only: str = Form("false"),
):
    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(DATA_DIR, job_id)
    in_dir  = os.path.join(job_dir, "in")
    out_dir = os.path.join(job_dir, "out")
    os.makedirs(in_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    saved = []
    for uf in files:
        sanitized_filename = sanitize_filename(os.path.basename(uf.filename or "unknown.pdf"))
        dst = os.path.join(in_dir, sanitized_filename)
        with open(dst, "wb") as f: f.write(await uf.read())
        saved.append(dst)

    total_pages = 0
    files_meta = []  # lista de dicts: {file: base_name, pages: int, id: sanitized_id}
    for p in saved:
        try:
            with fitz.open(p) as d:
                pages = d.page_count
                total_pages += pages
                base_name = os.path.splitext(os.path.basename(p))[0]
                sanitized_id = re.sub(r'\W+', '_', base_name)
                files_meta.append({"file": base_name, "pages": pages, "id": sanitized_id})
        except Exception:
            pass

    JOBS[job_id] = {
        "dir": job_dir, "in": saved, "out": out_dir,
        "compress_mode": (compress_mode.lower() == "true"),
        "metric_only": (metric_only.lower() == "true"),
        "total_pages": total_pages,
        "files_meta": files_meta,
    # Buffer de eventos para evitar perda de progresso antes do WS conectar
    "buffer": [],
    }

    if JOBS[job_id]["metric_only"]:
        asyncio.create_task(run_in_threadpool(process_metric_job, job_id))
    else:
        asyncio.create_task(run_in_threadpool(process_normal_job, job_id))

    return {"job_id": job_id, "total_pages": total_pages, "files": files_meta}

# ==== WebSocket ====
@app.websocket("/ws/{job_id}")
async def ws_progress(ws: WebSocket, job_id: str):
    await ws.accept()
    WS.setdefault(job_id, []).append(ws)
    try:
        job = JOBS.get(job_id, {})
        total = job.get("total_pages", 0)
        await ws.send_text(json.dumps({"event":"hello","data":{"total_pages":total}}))
        # envia metadados iniciais para evitar perda de file_start
        if job.get("files_meta"):
            await ws.send_text(json.dumps({"event":"init","data":{"files": job["files_meta"]}}))
        # Flush de eventos que aconteceram antes da conexão
        buf = job.get("buffer")
        if isinstance(buf, list):
            for ev in buf:
                try:
                    await ws.send_text(json.dumps(ev))
                except Exception:
                    pass
            # Marcar buffer como None para não acumular mais (próximos eventos vão direto)
            job["buffer"] = None
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        try: WS[job_id].remove(ws)
        except Exception: pass

# Dev runner
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)