# server.py
import os, re, uuid, json, time, asyncio, shutil
from typing import List, Dict, Any
from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
import anyio
import fitz  # PyMuPDF
from zipfile import ZipFile, ZIP_DEFLATED

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
ZERADO_PATTERNS = [
    r'CADASTRO:\s*\d+\s+([A-ZÀ-Ý ]{5,}?)(?=\s+CNPJ)',
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

def extract_name(text_clean: str, text_raw: str | None = None) -> str | None:
    # tenta padrões “clássicos”
    for p in NAME_PATTERNS:
        m = re.search(p, text_clean)
        if m:
            return m.group(1).strip()

    # tenta padrões do “PDF zerado”
    # primeiro no texto limpo (já está em MAIÚSCULAS e com espaços normalizados)
    for p in ZERADO_PATTERNS:
        m = re.search(p, text_clean)
        if m:
            return m.group(1).strip()

    # fallback opcional: tenta no texto cru (caso algum PDF venha com quebras estranhas)
    if text_raw:
        raw_up = re.sub(r'\s+', ' ', text_raw).strip().upper()
        for p in ZERADO_PATTERNS:
            m = re.search(p, raw_up)
            if m:
                return m.group(1).strip()

    return None

def sanitize_name_tokens(name: str) -> str | None:
    if not name: return None
    filtered = re.sub(r'[^A-ZÀ-Ý ]', ' ', name)
    # Permitir palavras com 2+ letras (incluindo DE, DA, DO, etc.)
    tokens = [t for t in filtered.split() if len(t) >= 2 and t not in STOPWORDS]
    if len(tokens) < 2: return None
    return ' '.join(tokens[:6])

def sanitize_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s if s else "ARQUIVO"

def generate_zip_filename(filenames: List[str]) -> str:
    # Se muitos arquivos, usar nome genérico curto
    if len(filenames) > 5:
        return "projetos_renomeados.zip"
    codes = set()
    for name in filenames:
        name_upper = os.path.splitext(os.path.basename(name))[0].upper()
        found_codes = re.findall(r'\b(\d{3,})\b', name_upper)
        codes.update(found_codes)
    if not codes:
        return "documentos_desmembrados.zip"
    sorted_codes = sorted(list(codes), key=int)
    base = "_&_".join(sorted_codes)
    return (base if len(base) < 60 else base[:57] + '...') + ".zip"

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
            # Cancelamento cooperativo
            job = JOBS.get(job_id)
            if job and job.get("cancel"):
                break
            page = doc.load_page(i)
            raw_page_text = page.get_text("text") or ""
            text = clean_text(raw_page_text)
            raw  = extract_name(text, raw_page_text)
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
                # Otimização: Usar deflate, clean e garbage=4; tentar linear se suportado,
                # caso contrário, faz fallback sem linear (algumas versões do PyMuPDF
                # não suportam linearisation e podem lançar erro).
                if compress:
                    try:
                        pdf_bytes = out_doc.write(garbage=4, deflate=True, clean=True, linear=True)
                    except Exception:
                        # Linearização não suportada — fallback seguro
                        pdf_bytes = out_doc.write(garbage=4, deflate=True, clean=True)
                else:
                    pdf_bytes = out_doc.write()
                out_doc.close()
                with open(out_path, "wb") as f: f.write(pdf_bytes)
            emit_from_worker(job_id, "page_done", {"file": base, "page": i+1, "newName": final})
    return stats

def make_zip(folder: str, zip_path: str):
    # Otimização: Usar ZIP_DEFLATED com compresslevel=6 para melhor compressão
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED, compresslevel=6) as zf:
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
            if job.get("cancel"):
                break
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
            if job.get("cancel"):
                break
        if job["in"] and not job.get("cancel"):
            zip_filename = generate_zip_filename(original_filenames)
            zip_path = os.path.join(base_out_dir, zip_filename)
            make_zip(root_processing_dir, zip_path)
            urls.append(f"/data/{job_id}/out/{zip_filename}")
        # Se cancelado, ainda podemos criar um zip parcial para o que foi gerado até agora
        if job.get("cancel"):
            try:
                zip_filename = "parcial_cancelado.zip"
                zip_path = os.path.join(base_out_dir, zip_filename)
                if not os.path.exists(zip_path):
                    make_zip(root_processing_dir, zip_path)
                urls.append(f"/data/{job_id}/out/{zip_filename}")
            except Exception:
                pass
            emit_from_worker(job_id, "cancelled", {"urls": urls, "summary": total_stats})
        else:
            emit_from_worker(job_id, "finished", {"urls": urls, "summary": total_stats})
        # agendar limpeza (best-effort) após alguns segundos
        try:
            asyncio.get_event_loop().call_later(120, lambda: shutil.rmtree(base_out_dir, ignore_errors=True))
        except Exception:
            pass
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
                    <!-- Compressão é obrigatória - checkbox removido -->
                    <div class="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-emerald-50 border border-emerald-200">
                        <svg class="w-4 h-4 text-emerald-600" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16.2L4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4L9 16.2z"/></svg>
                        <span class="font-medium text-emerald-700 text-xs">Compressão Ativada</span>
                    </div>
                </div>
                <div class="hidden sm:block"></div>
                <div class="flex items-center justify-center sm:justify-end gap-1.5">
                            <button id="btnGo" class="btn-primary inline-flex items-center gap-2 rounded-xl bg-orange-500 px-4 py-2.5 text-white text-sm font-medium leading-none hover:bg-orange-600 shadow-sm">
                <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M5.25 5.653c0-.856.917-1.398 1.665-.962l11.113 6.347a1.125 1.125 0 010 1.924L6.915 19.31a1.125 1.125 0 01-1.665-.962V5.653z"/></svg>
                Executar
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
        <div class="bg-white rounded-2xl w-full max-w-2xl max-h-[88vh] flex flex-col p-3 md:p-4 shadow-2xl relative">
      <div id="packagingOverlay" class="hidden absolute inset-0 z-30 backdrop-blur-sm bg-white/80 flex flex-col items-center justify-center gap-4">
          <div class="flex flex-col items-center gap-3">
              <div class="w-14 h-14 rounded-full border-4 border-sky-200 border-t-sky-600 animate-spin"></div>
              <div class="text-center">
                  <p class="font-semibold text-slate-700">Preparando download...</p>
                  <p class="text-xs text-slate-500">Compactando e organizando os arquivos</p>
              </div>
          </div>
      </div>
    <div id="progressHeader" class="flex items-center gap-2 pb-2 border-b mb-2 bg-gradient-to-r from-sky-50 to-white -mx-3 -mt-3 px-3 pt-2 rounded-t-2xl">
          <div class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" id="statusPulse"></div>
          <h3 id="progressTitle" class="text-lg font-semibold text-slate-800 tracking-tight truncate">Processando Arquivos...</h3>
      </div>
    <div id="immersiveProgress" class="mb-3 hidden">
            <div class="flex items-stretch justify-between gap-4 rounded-lg bg-slate-50/80 backdrop-blur-sm border p-3 shadow-inner">
                <div class="flex items-center gap-3">
                    <div class="relative w-14 h-14" id="circleContainer">
                        <div class="absolute inset-0 rounded-full bg-gradient-to-br from-sky-100 to-sky-200"></div>
                        <div class="absolute inset-0 rounded-full flex items-center justify-center">
                            <span id="circlePercent" class="text-sky-700 font-semibold text-sm transition-transform">0%</span>
                        </div>
                        <div id="circleRing" class="absolute inset-0 rounded-full" style="background:conic-gradient(#0284c7 0deg,#e2e8f0 0deg);"></div>
                        <div class="absolute inset-1.5 rounded-full bg-white"></div>
                        <div class="absolute inset-0 rounded-full" style="mask:radial-gradient(circle 52% at 50% 50%,transparent 60%,black 61%);"></div>
                    </div>
                    <div class="flex flex-col text-[11px] leading-tight min-w-[120px]">
                        <span class="text-slate-500">Páginas</span>
                        <span class="font-semibold text-slate-800 text-base"><span id="pagesDone">0</span>/<span id="pagesTotal">0</span></span>
                        <span class="text-slate-500">Arquivos: <span id="filesCount">0</span></span>
                    </div>
                </div>
                <div class="flex flex-col items-end text-[11px] leading-tight min-w-[70px]">
                    <span class="text-slate-500">Tempo</span>
                    <span id="timeElapsed" class="font-semibold text-slate-800 text-base">00:00</span>
                    <span id="eta" class="text-[10px] text-slate-500">--:--</span>
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
  
    <div id="detailsModal" class="hidden hidden-animated fixed inset-0 p-4 sm:p-8 flex items-center justify-center z-[60]">
    <div class="bg-white rounded-2xl w-full max-w-5xl max-h-[90vh] flex flex-col shadow-2xl overflow-hidden">
        <div class="flex items-center justify-between px-4 py-3 border-b bg-slate-50">
            <h3 class="font-semibold text-slate-800 text-base">Detalhes do Processamento</h3>
            <button id="detailsModalClose" class="p-1 rounded hover:bg-slate-200 text-slate-500 hover:text-slate-800" title="Fechar">
                <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M12 22C6.477 22 2 17.523 2 12S6.477 2 12 2s10 4.477 10 10-4.477 10-10 10zm0-11.414l-2.828-2.829-1.414 1.415L10.586 12l-2.828 2.828 1.414 1.415L12 13.414l2.828 2.829 1.414-1.415L13.414 12l2.828-2.828-1.414-1.415L12 10.586z"/></svg>
            </button>
        </div>
        <div class="flex flex-col lg:flex-row flex-grow min-h-0">
            <div class="lg:w-1/2 flex flex-col min-h-0 border-b lg:border-b-0 lg:border-r border-slate-200">
                <div class="px-4 py-2 text-xs font-semibold text-slate-500 tracking-wide">Arquivos</div>
                <div id="detailsFiles" class="flex-grow overflow-y-auto px-4 pb-4 space-y-1 text-xs"></div>
            </div>
            <div class="lg:w-1/2 flex flex-col min-h-0">
                <div class="px-4 py-2 text-xs font-semibold text-slate-500 tracking-wide flex items-center justify-between">
                    Logs
                    <button id="clearLogs" class="text-[10px] px-2 py-1 rounded bg-slate-100 hover:bg-slate-200 text-slate-600">Limpar</button>
                </div>
                <div id="detailsLogs" class="flex-grow overflow-y-auto px-4 pb-4 text-[11px] font-mono leading-snug whitespace-pre-wrap"></div>
            </div>
        </div>
    </div>
  </div>

<script>
const $ = (s) => document.querySelector(s);
// CORREÇÃO: Garante que todas as variáveis do modal de progresso sejam declaradas
const fileInput = $("#file"), dropZone = $("#drop"), fileHint = $("#fileHint"), selectionBox = $("#selectionBox");
const btnClear = $("#btnClear"), btnGo = $("#btnGo"), btnEscolher = $("#btnEscolher");
const selectionTitle = $("#selectionTitle"), cardGroupsContainer = $("#cardGroupsContainer"), actionsContainer = $("#actionsContainer");
// Compressão é sempre obrigatória
const historyBox = $("#historyBox"), historyLinks = $("#historyLinks");
const modal = $("#modal"), modalBackdrop = $("#modalBackdrop"), modalClose = $("#modalClose"), modalTitle = $("#modalTitle"), modalFrame = $("#modalFrame");
const progressModal = $("#progressModal"), progressTitle = $("#progressTitle"), perFileProgressContainer = $("#perFileProgressContainer"), summaryContainer = $("#summaryContainer"), logDetails = $("#logDetails"), logContainer = $("#logContainer"), resultContainer = $("#resultContainer"), statusPulse = $("#statusPulse");
const detailsModal = document.getElementById('detailsModal');
const detailsModalClose = document.getElementById('detailsModalClose');
const detailsFiles = document.getElementById('detailsFiles');
const detailsLogs = document.getElementById('detailsLogs');
const clearLogsBtn = document.getElementById('clearLogs');
const immersiveProgress = document.getElementById('immersiveProgress');
const circleRing = document.getElementById('circleRing');
const circlePercent = document.getElementById('circlePercent');
const pagesDoneEl = document.getElementById('pagesDone');
const pagesTotalEl = document.getElementById('pagesTotal');
const filesCountEl = document.getElementById('filesCount');
const timeElapsedEl = document.getElementById('timeElapsed');
const etaEl = document.getElementById('eta');
const packagingOverlay = document.getElementById('packagingOverlay');
let packagingShown = false;
let pickedFiles = [], activeModalUrl = null, filesProgress = {}, currentJobId = null, jobCancelled = false, cancelJobBtn = null;

const bytesToSize = b => b < 1024 ? b + " B" : (b < 1048576 ? (b / 1024).toFixed(1) + " KB" : (b / 1048576).toFixed(1) + " MB");

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
    } else {
        fileHint.classList.add('hidden');
        actionsContainer.style.maxHeight = "500px"; 
        actionsContainer.style.opacity = 1;
        actionsContainer.style.paddingTop = "1.5rem";
        actionsContainer.style.borderTopWidth = "1px";
    }
}

function clearSelection() { 
    cardGroupsContainer.innerHTML = ""; 
    pickedFiles = []; 
    fileInput.value = ""; 
    updateSelectionUI(); 
}


function openDetailsModal(summary){
    if(!detailsModal) return;
    detailsFiles.innerHTML='';
    (summary?.files||[]).forEach(f=>{
        const el = document.createElement('div');
        el.className='flex items-center justify-between gap-2 p-2 rounded border border-slate-200 bg-white hover:bg-slate-50';
        el.innerHTML=`<span class='truncate font-medium text-slate-700' title='${f.file}.pdf'>${f.file}</span>
                       <span class='text-[10px] text-slate-500'>${f.pages} pág · <span class='text-sky-600'>${f.renamed}</span> ren</span>`;
        detailsFiles.appendChild(el);
    });
    detailsLogs.textContent = logContainer.innerText || '';
    detailsModal.classList.remove('hidden');
}

detailsModalClose?.addEventListener('click', ()=>{ detailsModal.classList.add('hidden'); });
clearLogsBtn?.addEventListener('click', ()=>{ detailsLogs.textContent=''; });

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
    modalBackdrop.style.pointerEvents = 'auto';
    modalElement.classList.remove('hidden');
    setTimeout(() => {
      modalBackdrop.classList.remove('opacity-0');
      modalElement.classList.remove('hidden-animated');
      modalElement.classList.add('visible-animated');
    }, 10);
    try { history.pushState({ modal: modalElement.id }, '', location.href); } catch(e){}
  } else {
    modalBackdrop.classList.add('opacity-0');
    modalElement.classList.remove('visible-animated');
    modalElement.classList.add('hidden-animated');
    setTimeout(() => {
      modalElement.classList.add('hidden');
      const allClosed = modal.classList.contains('hidden') && progressModal.classList.contains('hidden');
      if (allClosed) {
        modalBackdrop.classList.add('hidden');
        modalBackdrop.style.pointerEvents = 'none';
      }
    }, 300);
  }
}

window.addEventListener('popstate', () => {
  // Fecha qualquer modal aberto e limpa backdrop
  if (!modal.classList.contains('hidden')) {
    toggleModal(modal, false);
    // limpa iframe/URL blob
    if (activeModalUrl) { URL.revokeObjectURL(activeModalUrl); activeModalUrl = null; }
    modalFrame.src = 'about:blank';
  }
  if (!progressModal.classList.contains('hidden')) {
    toggleModal(progressModal, false);
  }
  // Garante que o backdrop não fique “travando” os cliques
  modalBackdrop.classList.add('hidden');
  modalBackdrop.style.pointerEvents = 'none';
});


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
    const incoming = Array.from(fileList).filter(f => f.name.toLowerCase().endsWith('.pdf'));
    if(incoming.length === 0){
        if(pickedFiles.length===0){ fileHint.textContent = "⚠️ Nenhum arquivo PDF válido foi selecionado."; }
        updateSelectionUI();
        return;
    }
    // mapa de existentes para evitar duplicados (usa trio nome+size+lastModified)
    const existingSet = new Set(pickedFiles.map(f=>`${f.name}__${f.size}__${f.lastModified}`));
    let added = 0;
    incoming.forEach(f=>{
        const key = `${f.name}__${f.size}__${f.lastModified}`;
        if(!existingSet.has(key)){
            pickedFiles.push(f);
            existingSet.add(key);
            added++;
        }
    });
    if(pickedFiles.length === 0){
        fileHint.textContent = "⚠️ Nenhum arquivo PDF válido foi selecionado.";
    }
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
// Sem botão fechar durante processamento
modalBackdrop.onclick = () => {
    toggleModal(modal, false);
    toggleModal(progressModal, false);
};

async function runJob(files, metricOnly) {
    perFileProgressContainer.innerHTML = "";
    summaryContainer.innerHTML = "";
    summaryContainer.classList.add("hidden");
    logDetails.open = false;
    logDetails.querySelector('summary').innerHTML = "Ver logs detalhados";
    logDetails.style.display = 'none';
    progressTitle.textContent = metricOnly ? "Testando Desempenho..." : "Processando Arquivos...";
    filesProgress = {};
    toggleModal(progressModal, true);
    immersiveProgress.classList.add('hidden');
    circlePercent.textContent = '0%';
    circleRing.style.background = 'conic-gradient(#0284c7 0deg,#e2e8f0 0deg)';
    pagesDoneEl.textContent = '0';
    pagesTotalEl.textContent = '0';
    filesCountEl.textContent = '0';
    timeElapsedEl.textContent = '00:00';
    let globalTotalPages = 0; let globalDonePages = 0; let t0 = performance.now();
    let tickInterval = null;
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
        // ETA simples
        if(globalDonePages>0 && globalTotalPages>0){
            const rate = globalDonePages/elapsed; // páginas por segundo
            if(rate>0){
                const remainingPages = globalTotalPages - globalDonePages;
                const remainingSec = remainingPages / rate;
                const rm = Math.floor(remainingSec/60).toString().padStart(2,'0');
                const rs = Math.floor(remainingSec%60).toString().padStart(2,'0');
                etaEl.textContent = `${rm}:${rs}`;
            }
        }
        // mini stats (se mini-mode)
    }

    const modalRoot = document.getElementById('progressModal');
    immersiveProgress.classList.add('hidden');
    perFileProgressContainer.className = 'flex-grow overflow-y-auto scrollbar-thin text-xs grid gap-y-0.5 gap-x-4' ;
    perFileProgressContainer.style.gridTemplateColumns = 'repeat(auto-fill,minmax(300px,1fr))';
        files.forEach(file => {
                const fileBasename = file.name.replace(/\.pdf$/i, '').replace(/[^A-Za-z0-9]+/g, '_');
                const row = document.createElement('div');
                row.id = `progress-${fileBasename}`;
                row.className = 'fileRow flex items-center gap-2 py-1 px-1 pr-2 status-pending border-b border-slate-100 last:border-none';
                row.innerHTML = `
                    <span class="statusIcon flex-shrink-0 inline-flex items-center justify-center w-4 h-4 rounded-full bg-yellow-400 text-white text-[9px] font-bold">!</span>
                    <span class="flex-grow font-medium text-slate-700 truncate" title="${file.name}">${file.name}</span>
                    <span class="count font-mono text-slate-500 text-[11px] w-20 text-right">Aguardando...</span>`;
                perFileProgressContainer.appendChild(row);
        });

    const fd = new FormData();
    files.forEach(f => fd.append("files", f));
    // Compressão é sempre obrigatória
    fd.append("compress_mode", "true");
    fd.append("metric_only", metricOnly ? "true" : "false");

    let job_id = null;
    try {
        const res = await fetch("/api/process", { method: "POST", body: fd });
        if(!res.ok){
            let detail = '';
            try { detail = await res.text(); } catch(e){}
            summaryContainer.innerHTML = `<div class='p-4 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700'>Erro ao iniciar processamento (HTTP ${res.status}).<br/><pre class='whitespace-pre-wrap text-xs mt-2'>${detail.replace(/[<>]/g,'')}</pre></div>`;
            summaryContainer.classList.remove('hidden');
            toggleModal(progressModal, true);
            return;
        }
        const data = await res.json();
    job_id = data.job_id;
    currentJobId = job_id;
    jobCancelled = false;
    } catch(fetchErr){
        summaryContainer.innerHTML = `<div class='p-4 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700'>Falha na requisição /api/process.<br/><code>${(fetchErr&&fetchErr.message)||fetchErr}</code></div>`;
        summaryContainer.classList.remove('hidden');
        toggleModal(progressModal, true);
        return;
    }
    // debug log removido (PEGA-BUG)
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/${job_id}`);
    // Botão cancelar no rodapé
    resultContainer.innerHTML='';
    cancelJobBtn = document.createElement('button');
    cancelJobBtn.className='w-full inline-flex items-center justify-center gap-2 rounded-xl bg-red-600 hover:bg-red-700 text-white font-medium px-4 py-2 text-sm shadow';
    cancelJobBtn.innerHTML='<svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 1010 10A10.011 10.011 0 0012 2zm3.707 12.293a1 1 0 01-1.414 1.414L12 13.414l-2.293 2.293a1 1 0 01-1.414-1.414L10.586 12 8.293 9.707a1 1 0 011.414-1.414L12 10.586l2.293-2.293a1 1 0 011.414 1.414L13.414 12z"/></svg><span>Cancelar processamento</span>';
    cancelJobBtn.onclick = async ()=>{
        if(!currentJobId || jobCancelled) return;
        jobCancelled = true;
        cancelJobBtn.disabled = true;
        cancelJobBtn.innerHTML = '<span class="animate-pulse">Cancelando...</span>';
        cancelJobBtn.classList.add('opacity-80');
        try { await fetch(`/api/cancel/${currentJobId}`, { method: 'POST' }); } catch(e){}
    };
    resultContainer.appendChild(cancelJobBtn);

    ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
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
                                                el.className = 'fileRow flex items-center gap-2 py-1.5 px-1 pr-2 status-processing';
                                                el.innerHTML = `
                                                    <span class="statusIcon flex-shrink-0 inline-flex items-center justify-center w-4 h-4 rounded-full bg-sky-500 text-white text-[9px]">⟳</span>
                                                    <span class="flex-grow font-medium text-slate-700 truncate" title="${f.file}.pdf">${f.file}.pdf</span>
                                                    <span class="count font-mono text-slate-500 text-[11px] w-20 text-right">0/${f.pages}</span>`;
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
                    const c0 = el.querySelector('.count');
                    if(c0) c0.textContent = `0/${msg.data.pages}`;
                    el.classList.remove('status-pending');
                    el.classList.add('status-processing');
                    el.classList.add('bg-sky-50');
                    const icon = el.querySelector('.statusIcon');
                    if(icon){
                        icon.innerHTML = '<svg class="w-4 h-4 text-sky-600 animate-spin" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v3a5 5 0 00-5 5H4z"/></svg>';
                        icon.className = 'statusIcon flex-shrink-0 inline-flex items-center justify-center w-4 h-4';
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
                    const countEl = el.querySelector('.count');
                    if(countEl){
                        const prevW = countEl.offsetWidth; // largura fixa para não provocar reflow externo
                        countEl.style.display='inline-block';
                        countEl.style.width = prevW? prevW+'px':'auto';
                        countEl.textContent = `${fp.done}/${fp.total}`;
                        countEl.animate([
                            { transform:'scale(1)', color:'#0369a1' },
                            { transform:'scale(1.15)', color:'#0ea5e9' },
                            { transform:'scale(1)', color:'#0369a1' }
                        ], { duration:220, easing:'ease-out' });
                        if(fp.done === fp.total){
                            countEl.classList.add('text-emerald-600','font-semibold');
                            countEl.animate([
                                { transform:'scale(1)', color:'#059669' },
                                { transform:'scale(1.22)', color:'#10b981' },
                                { transform:'scale(1)', color:'#059669' }
                            ], { duration:300, easing:'ease-out' });
                        }
                    }
                    // completed?
                    if(fp.done === fp.total){
                        el.classList.remove('status-processing','bg-sky-50');
                        el.classList.add('status-done','bg-emerald-50');
                        const icon = el.querySelector('.statusIcon');
                        if(icon){
                            icon.textContent = '✓';
                            icon.className = 'statusIcon flex-shrink-0 inline-flex items-center justify-center w-4 h-4 rounded-full bg-emerald-500 text-white text-[9px]';
                        }
                    }
                }
                pagesDoneEl.textContent = globalDonePages;
                pagesTotalEl.textContent = globalTotalPages;
                // registrar amostra para sparkline (últimos ~10s)
                const nowT = (performance.now()-t0)/1000;
                updateVisual();
                // se todas as páginas concluídas mas ainda não veio finished, mostrar overlay
                if(!packagingShown && globalDonePages === globalTotalPages && globalTotalPages>0){
                    packagingShown = true;
                    packagingOverlay?.classList.remove('hidden');
                }
                if (!metricOnly) {
                    const logEntry = document.createElement('p');
                    logEntry.className = "text-sm text-slate-600 border-b border-slate-200 pb-1 mb-1 font-mono";
                    logEntry.innerHTML = `Pág. <b>${msg.data.page}</b> de <i>${msg.data.file}.pdf</i>  <span class=\"text-slate-400\"> ➤ </span> <span class=\"font-medium text-emerald-700\">${msg.data.newName}.pdf</span>`;
                    logContainer.appendChild(logEntry);
                    logContainer.scrollTop = logContainer.scrollHeight;
                }
                break; }
case "finished": {
                if(packagingOverlay){ packagingOverlay.classList.add('hidden'); }
                packagingShown = false;
                // Parar timers
                if (tickInterval) { clearInterval(tickInterval); tickInterval = null; }
                // Capturar summary
                const summary = msg.data.summary || {renamed:0, manual:0, files:[]};
                const totalPagesGlobal = summary.files.reduce((a,f)=>a+f.pages,0);
                // Limpar área visual anterior
                const headerEl = document.getElementById('progressHeader');
                if(headerEl) headerEl.classList.add('hidden');
                immersiveProgress.classList.add('hidden');
                summaryContainer.classList.add('hidden');
                // Construir novo layout: uma linha global + linhas por arquivo
                perFileProgressContainer.innerHTML = '';
                perFileProgressContainer.className = 'flex-grow overflow-y-auto scrollbar-thin divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white';
                // Linha global
                const globalLine = document.createElement('div');
                globalLine.className='flex items-center gap-4 px-3 py-2 text-sm bg-sky-50 font-medium text-slate-700 sticky top-0';
                globalLine.innerHTML = `<span class='flex-1'>Resumo Geral</span>
                                <span class='w-48 text-right font-mono text-[12px]'><span class='text-sky-700 font-semibold'>${summary.renamed}</span>/<span class='${summary.manual>0?'text-amber-600 font-semibold':'text-slate-400'}'>${summary.manual}</span> · ${totalPagesGlobal} pág.</span>`;
                perFileProgressContainer.appendChild(globalLine);
                // Linhas de arquivos
                (summary.files||[]).forEach(f=>{
                    const line = document.createElement('div');
                    line.className='flex items-center gap-4 px-3 py-1.5 text-[13px] hover:bg-slate-50';
                    const statusClass = f.manual === 0 ? 'text-emerald-600' : (f.renamed>0 ? 'text-amber-600' : 'text-red-600');
                    const statusLabel = f.manual === 0 ? '100% renomeado' : (f.renamed>0 ? 'Parcial' : 'Nenhuma página nomeada');
                    line.innerHTML = `
                                       <span class='w-4 h-4 flex items-center justify-center text-slate-400'>📄</span>
                                       <span class='flex-1 truncate font-medium text-slate-700' title='${f.file}.pdf'>${f.file}.pdf</span>
                                       <span class='w-40 text-right font-mono text-[11px] text-slate-600'><span class='text-sky-600 font-semibold'>${f.renamed}</span>/<span class='${f.manual>0?'text-amber-600 font-semibold':'text-slate-400'}'>${f.manual}</span> · ${f.pages} pág.</span>
                                       <span class='w-32 text-right text-[11px] ${statusClass}'>${statusLabel}</span>`;
                    perFileProgressContainer.appendChild(line);
                });
                // Ajustar altura
                perFileProgressContainer.style.maxHeight='55vh';
                progressTitle.textContent='';
                // Botão download (auto + manual) e LÓGICA DE HISTÓRICO
                resultContainer.innerHTML='';
                historyBox.classList.remove("hidden");
                let autoTriggered = false;
                for (const url of msg.data.urls) {
                    const filename = url.split("/").pop();
                    // Auto-download
                    if(!autoTriggered){
                        const a = document.createElement('a'); a.href = url; a.download = filename; a.style.display='none'; document.body.appendChild(a); a.click(); setTimeout(()=>a.remove(),800);
                        autoTriggered = true;
                    }
                    // Botão de download no modal
                    const dlBtn = document.createElement('a');
                    dlBtn.className = 'inline-flex items-center justify-center gap-2 rounded-xl text-white px-4 py-2 text-base font-medium bg-emerald-600 hover:bg-emerald-700 w-full';
                    dlBtn.href = url; dlBtn.innerHTML = `<svg class=\"w-6 h-6\" viewBox=\"0 0 24 24\" fill=\"currentColor\"><path d=\"M12 15a1 1 0 01-.7-.29l-4-4a1 1 0 111.4-1.42L12 12.59l3.3-3.3a1 1 0 111.4 1.42l-4 4a1 1 0 01-.7.29zM12 3a9 9 0 109 9 9 9 0 00-9-9zm0 16a7 7 0 117-7 7 7 0 01-7 7z\"/></svg><span>Baixar novamente ${filename}</span>`;
                    resultContainer.appendChild(dlBtn);

                    // ### INÍCIO DA CORREÇÃO DE HISTÓRICO ###
                    const historyItem = document.createElement('li');
                    historyItem.className = 'bg-white border border-slate-200 rounded-xl p-3 flex items-center justify-between gap-3 shadow-sm card-animate-in';
                    historyItem.innerHTML = `
                        <div class="flex items-center gap-3 min-w-0">
                            <div class="flex-shrink-0 w-10 h-10 flex items-center justify-center bg-sky-100 text-sky-600 rounded-lg">
                                <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path fill-rule="evenodd" d="M2.25 1.5A2.25 2.25 0 000 3.75v16.5A2.25 2.25 0 002.25 22.5h19.5A2.25 2.25 0 0024 20.25V7.5a2.25 2.25 0 00-2.25-2.25h-9a.75.75 0 01-.53-.22L9.22 2.47a2.25 2.25 0 00-1.59-.64H2.25zm.36 18.06a.75.75 0 00.75-.75V3.75h4.19c.47 0 .93.19 1.25.53l2.25 2.25c.32.32.78.53 1.25.53h9.01v12.75a.75.75 0 01-.75.75H2.61z" clip-rule="evenodd" /></svg>
                            </div>
                            <div class="min-w-0">
                                <p class="font-semibold text-slate-800 truncate" title="${filename}">${filename}</p>
                                <p class="text-xs text-slate-500">Concluído às ${new Date().toLocaleTimeString('pt-BR')}</p>
                            </div>
                        </div>
                        <a href="${url}" download="${filename}" title="Baixar ${filename}" class="flex-shrink-0 inline-flex items-center justify-center w-9 h-9 rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200 hover:text-slate-800 transition-colors">
                            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M12 16.5a.75.75 0 01-.53-.22l-4.5-4.5a.75.75 0 011.06-1.06L11.25 14.19V5.25a.75.75 0 011.5 0v8.94l3.22-3.22a.75.75 0 111.06 1.06l-4.5 4.5a.75.75 0 01-.53.22zm-7.5 1.5A2.25 2.25 0 006.75 20.25h10.5a2.25 2.25 0 002.25-2.25a.75.75 0 011.5 0A3.75 3.75 0 0117.25 21.75H6.75A3.75 3.75 0 013 18a.75.75 0 011.5 0z"/></svg>
                        </a>
                    `;
                    historyLinks.prepend(historyItem);
                    // ### FIM DA CORREÇÃO DE HISTÓRICO ###
                }
                const closeBtn = document.createElement('button');
                closeBtn.className='w-full inline-flex items-center justify-center gap-2 rounded-xl bg-slate-600 hover:bg-slate-700 text-white font-medium px-4 py-2 text-sm';
                closeBtn.textContent='Fechar';
                closeBtn.onclick=()=>toggleModal(progressModal,false);
                resultContainer.appendChild(closeBtn);
                ws.close();
                clearSelection();
                break; 
            }
            case "metric":
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
                if(packagingOverlay){ packagingOverlay.classList.add('hidden'); }
                packagingShown = false;
                progressTitle.textContent = "Erro no Processamento";
                if (tickInterval) { clearInterval(tickInterval); tickInterval = null; updateVisual(); }
                circleRing.style.background = 'conic-gradient(#dc2626 360deg,#e2e8f0 360deg)';
                circlePercent.classList.add('text-red-600');
                summaryContainer.innerHTML = `<p class="text-red-600 p-4 bg-red-50 rounded-lg">${msg.data.message}</p>`;
                summaryContainer.classList.remove("hidden");
                ws.close();
                // Substitui por botão fechar
                resultContainer.innerHTML='';
                const closeErr = document.createElement('button');
                closeErr.className='w-full inline-flex items-center justify-center gap-2 rounded-xl bg-slate-600 hover:bg-slate-700 text-white font-medium px-4 py-2 text-sm';
                closeErr.textContent='Fechar';
                closeErr.onclick=()=>toggleModal(progressModal,false);
                resultContainer.appendChild(closeErr);
                break;
            case "hello":
                // já recebido no início: pode conter total_pages
                if (typeof msg.data.total_pages === 'number') {
                    // Só mostra depois do primeiro file_start para evitar 0/0
                }
                break;
            case "cancelled": {
                if(packagingOverlay){ packagingOverlay.classList.add('hidden'); }
                packagingShown = false;
                if (tickInterval) { clearInterval(tickInterval); tickInterval = null; }
                const summary = msg.data.summary || {renamed:0, manual:0, files:[]};
                const totalPagesGlobal = summary.files.reduce((a,f)=>a+f.pages,0);
                const headerEl = document.getElementById('progressHeader');
                if(headerEl) headerEl.classList.add('hidden');
                immersiveProgress.classList.add('hidden');
                summaryContainer.classList.add('hidden');
                perFileProgressContainer.innerHTML='';
                perFileProgressContainer.className='flex-grow overflow-y-auto scrollbar-thin divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white';
                const globalLine = document.createElement('div');
                globalLine.className='flex items-center gap-4 px-3 py-2 text-sm bg-amber-50 font-medium text-slate-700 sticky top-0';
                globalLine.innerHTML = `<span class='flex-1'>Processamento Cancelado</span>
                    <span class='w-48 text-right font-mono text-[12px]'><span class='text-sky-700 font-semibold'>${summary.renamed}</span>/<span class='${summary.manual>0?'text-amber-600 font-semibold':'text-slate-400'}'>${summary.manual}</span> · ${totalPagesGlobal} pág.</span>`;
                perFileProgressContainer.appendChild(globalLine);
                (summary.files||[]).forEach(f=>{
                    const line = document.createElement('div');
                    line.className='flex items-center gap-4 px-3 py-1.5 text-[13px] hover:bg-slate-50';
                    const statusClass = f.manual === 0 ? 'text-emerald-600' : (f.renamed>0 ? 'text-amber-600' : 'text-red-600');
                    const statusLabel = f.manual === 0 ? '100% renomeado' : (f.renamed>0 ? 'Parcial' : 'Nenhuma página nomeada');
                    line.innerHTML = `
                       <span class='w-4 h-4 flex items-center justify-center text-slate-400'>📄</span>
                       <span class='flex-1 truncate font-medium text-slate-700' title='${f.file}.pdf'>${f.file}.pdf</span>
                       <span class='w-40 text-right font-mono text-[11px] text-slate-600'><span class='text-sky-600 font-semibold'>${f.renamed}</span>/<span class='${f.manual>0?'text-amber-600 font-semibold':'text-slate-400'}'>${f.manual}</span> · ${f.pages} pág.</span>
                       <span class='w-32 text-right text-[11px] ${statusClass}'>${statusLabel}</span>`;
                    perFileProgressContainer.appendChild(line);
                });
                resultContainer.innerHTML='';
                historyBox.classList.remove('hidden');
                for (const url of msg.data.urls||[]) {
                    const filename = url.split("/").pop();
                    const dlBtn = document.createElement('a');
                    dlBtn.className = 'inline-flex items-center justify-center gap-2 rounded-xl text-white px-4 py-2 text-base font-medium bg-amber-600 hover:bg-amber-700 w-full';
                    dlBtn.href = url; dlBtn.innerHTML = `<svg class=\"w-5 h-5\" viewBox=\"0 0 24 24\" fill=\"currentColor\"><path d=\"M12 15a1 1 0 01-.7-.29l-4-4a1 1 0 111.4-1.42L12 12.59l3.3-3.3a1 1 0 111.4 1.42l-4 4a1 1 0 01-.7.29zM12 3a9 9 0 109 9 9 9 0 00-9-9zm0 16a7 7 0 117-7 7 7 0 01-7 7z\"/></svg><span>Baixar parcial ${filename}</span>`;
                    resultContainer.appendChild(dlBtn);
                }
                const closeBtn = document.createElement('button');
                closeBtn.className='w-full inline-flex items-center justify-center gap-2 rounded-xl bg-slate-600 hover:bg-slate-700 text-white font-medium px-4 py-2 text-sm';
                closeBtn.textContent='Fechar';
                closeBtn.onclick=()=>toggleModal(progressModal,false);
                resultContainer.appendChild(closeBtn);
                ws.close();
                clearSelection();
                break; }
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
    metric_only: str = Form("false"),
):
    # Compressão é sempre obrigatória
    compress_mode = True
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
        "compress_mode": compress_mode,  # Sempre True - compressão obrigatória
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

@app.post("/api/cancel/{job_id}")
async def cancel_endpoint(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return {"status":"unknown"}
    job["cancel"] = True
    return {"status":"cancelled"}

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