# server.py
import os, re, uuid, json, time, zipfile, asyncio
from typing import List, Dict, Any
from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
import anyio
import uvicorn
import fitz  # PyMuPDF

# ==== Config ====
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

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

async def emit(job_id: str, event: str, payload: dict):
    if job_id not in WS: return
    dead = []
    for ws in WS.get(job_id, []):
        try:
            await ws.send_text(json.dumps({"event": event, "data": payload}))
        except Exception:
            dead.append(ws)
    for d in dead:
        try: WS[job_id].remove(d)
        except ValueError: pass

def emit_from_worker(job_id: str, event: str, payload: dict):
    try:
        anyio.from_thread.run(emit, job_id, event, payload)
    except Exception:
        pass

# ==== Core (Sequencial e Estável) ====
def process_pdf_to_folder(src_pdf: str, out_dir: str, job_id: str, compress: bool, is_metric_run: bool = False):
    base = os.path.splitext(os.path.basename(src_pdf))[0]
    if not is_metric_run:
        os.makedirs(out_dir, exist_ok=True)
    
    stats = {"renamed": 0, "manual": 0, "manual_pages": []}
    
    with fitz.open(src_pdf) as doc:
        total = doc.page_count
        emit_from_worker(job_id, "file_start", {"file": base, "pages": total})
        for i in range(total):
            page = doc.load_page(i)
            text = clean_text(page.get_text("text"))
            raw  = extract_name(text)
            final = sanitize_name_tokens(raw) if raw else None
            
            is_manual = False
            if not final:
                final = f"MANUAL_{base}_{i+1}"
                is_manual = True
            final = sanitize_filename(final)

            if is_manual:
                stats["manual"] += 1
                stats["manual_pages"].append(f"Página {i+1} de {base}.pdf")
            else:
                stats["renamed"] += 1

            if not is_metric_run:
                out_path = os.path.join(out_dir, f"{final}.pdf")
                k = 1
                while os.path.exists(out_path):
                    out_path = os.path.join(out_dir, f"{final}_{k}.pdf"); k += 1
                
                out_doc = fitz.open()
                out_doc.insert_pdf(doc, from_page=i, to_page=i)
                pdf_bytes = out_doc.write(garbage=4, deflate=True, clean=True) if compress else out_doc.write()
                out_doc.close()
                with open(out_path, "wb") as f:
                    f.write(pdf_bytes)

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
        base_out_dir = job["out"]
        compress = job["compress_mode"]
        urls = []
        
        total_stats = {"renamed": 0, "manual": 0, "manual_pages": []}

        for src_pdf_path in job["in"]:
            file_basename = os.path.splitext(os.path.basename(src_pdf_path))[0]
            file_out_dir = os.path.join(base_out_dir, file_basename)
            
            file_stats = process_pdf_to_folder(src_pdf_path, file_out_dir, job_id, compress, is_metric_run=False)
            
            total_stats["renamed"] += file_stats["renamed"]
            total_stats["manual"] += file_stats["manual"]
            total_stats["manual_pages"].extend(file_stats["manual_pages"])
            
            zip_filename = f"{file_basename}.zip"
            zip_path = os.path.join(base_out_dir, zip_filename)
            make_zip(file_out_dir, zip_path)
            
            urls.append(f"/data/{job_id}/out/{zip_filename}")
        
        emit_from_worker(job_id, "finished", {"urls": urls, "summary": total_stats})
    except Exception as e:
        emit_from_worker(job_id, "error", {"message": str(e)})

def process_metric_job(job_id: str):
    try:
        job = JOBS[job_id]
        target, compress = job["in"][0], job["compress_mode"]
        
        t0 = time.perf_counter()
        process_pdf_to_folder(target, None, job_id, compress, is_metric_run=True)
        elapsed = round(time.perf_counter() - t0, 2)
        
        with fitz.open(target) as d: pages = d.page_count
        
        ram = 0.0
        try:
            import psutil
            ram = round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
        except Exception: pass

        emit_from_worker(job_id, "metric", {"pages": pages, "time": elapsed, "ram": ram})
    except Exception as e:
        emit_from_worker(job_id, "error", {"message": str(e)})

# ==== UI e API ====
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse("""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Folha Ponto — Web</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  .scrollbar-thin::-webkit-scrollbar{width:6px;}
  .scrollbar-thin::-webkit-scrollbar-thumb{background:#94a3b8;border-radius:3px;}
  .scrollbar-thin::-webkit-scrollbar-track{background:#e2e8f0;}
  .transition-all{transition:all 0.3s ease-in-out;}
  .max-h-0{max-height:0;}
  .max-h-screen{max-height:100vh;}
</style>
</head>
<body class="bg-slate-50 text-slate-900 text-[15px]">
  <div class="max-w-6xl mx-auto p-4 md:p-8">
    <header class="mb-6 md:mb-10 text-center">
      <h1 class="text-3xl md:text-4xl font-semibold tracking-tight">Folha Ponto — Web</h1>
    </header>

    <section class="bg-white rounded-2xl shadow-sm ring-1 ring-black/5 p-5 md:p-8 space-y-8">
      <div class="text-center">
        <div id="drop" class="mx-auto max-w-2xl border-2 border-dashed rounded-2xl p-6 md:p-8 text-center bg-rose-50/40 hover:bg-rose-50 transition-all">
          <div class="flex items-center justify-center gap-3 text-rose-700">
            <svg class="w-7 h-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5a4.5 4.5 0 004.5 4.5h9A4.5 4.5 0 0021 16.5m-9-12v12m0-12l-3 3m3-3l3 3"/>
            </svg>
            <span>Arraste arquivos aqui ou</span>
            <button id="btnEscolher" class="inline-flex items-center gap-2 rounded-xl bg-rose-600 text-white px-4 py-2 cursor-pointer hover:bg-rose-700">
              <svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M12 3c.414 0 .75.336.75.75V15h3.19a.75.75 0 01.53 1.28l-3.94 3.94a.75.75 0 01-1.06 0l-3.94-3.94a.75.75 0 01.53-1.28H11.25V3.75c0-.414.336-.75.75-.75z"/></svg>
              <span>Escolha arquivos</span>
            </button>
          </div>
          <input id="file" type="file" accept="application/pdf" multiple class="sr-only" />
          <p id="fileHint" class="mt-3 text-slate-500 text-sm">Nenhum arquivo selecionado.</p>
        </div>
      </div>

      <div id="selectionBox" class="hidden space-y-3">
        <div class="flex items-center justify-between">
          <span class="font-semibold">Selecionados <span id="selCount" class="text-slate-500 font-normal">(0)</span></span>
          <button id="btnClear" class="text-sm text-slate-600 hover:text-slate-900">Limpar</button>
        </div>
        <div id="cards" class="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(240px,1fr))]"></div>
      </div>

      <div id="actionsContainer" class="max-h-0 overflow-hidden transition-all duration-500">
        <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 items-center border-t pt-6">
            <label class="inline-flex items-center gap-3">
              <input id="compressMode" type="checkbox" class="h-5 w-5 rounded border-slate-300">
              <span>Compactar PDFs (menor qualidade)</span>
            </label>
            <div class="hidden lg:block"></div>
            <div class="flex items-center justify-center sm:justify-end gap-2">
              <button id="btnGo" class="inline-flex items-center gap-2 rounded-2xl bg-rose-600 px-5 py-3 text-white text-lg font-medium hover:bg-rose-700">
                <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M5.25 5.653c0-.856.917-1.398 1.665-.962l11.113 6.347a1.125 1.125 0 010 1.924L6.915 19.31a1.125 1.125 0 01-1.665-.962V5.653z"/></svg>
                Executar
              </button>
              <button id="btnAdmin" class="hidden inline-flex items-center gap-2 rounded-2xl bg-slate-800 px-5 py-3 text-white text-lg font-medium hover:bg-slate-700">
                  <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M4 4.5A2.5 2.5 0 016.5 2H17.5A2.5 2.5 0 0120 4.5v2.828a.5.5 0 01-.146.354l-2.5 2.5a.5.5 0 01-.708 0l-2.5-2.5A.5.5 0 0114 7.328V4.5a.5.5 0 00-.5-.5h-3a.5.5 0 00-.5.5v2.828a.5.5 0 01-.146.354l-2.5 2.5a.5.5 0 01-.708 0l-2.5-2.5A.5.5 0 014 7.328V4.5zM4 12.5A2.5 2.5 0 016.5 10h11a2.5 2.5 0 012.5 2.5v7A2.5 2.5 0 0117.5 22H6.5A2.5 2.5 0 014 19.5v-7zM14 15.5a1 1 0 10-2 0 1 1 0 002 0z"/></svg>
                  Executar Teste
              </button>
              <button id="btnDebug" title="Acesso Restrito" class="p-3 rounded-2xl text-slate-500 hover:text-slate-800 hover:bg-slate-100">
                <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.25a4.5 4.5 0 00-4.5 4.5v.75H6a3 3 0 00-3 3v6.75A2.25 2.25 0 005.25 19.5h13.5A2.25 2.25 0 0021 17.25V10.5a3 3 0 00-3-3h-1.5V6.75a4.5 4.5 0 00-4.5-4.5z"/></svg>
              </button>
            </div>
        </div>
      </div>
      
      <div id="historyBox" class="hidden border-t pt-6 mt-8 space-y-4">
          <h3 class="text-xl font-semibold text-center text-slate-700">Histórico de Arquivos Prontos</h3>
          <ul id="historyLinks" class="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(280px,1fr))]"></ul>
      </div>

    </section>
  </div>

  <div id="modalBackdrop" class="hidden fixed inset-0 bg-black/60 z-40 transition-opacity"></div>
  <div id="modal" class="hidden fixed inset-0 p-4 sm:p-8 flex items-center justify-center z-50">
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

  <div id="progressModal" class="hidden fixed inset-0 p-4 sm:p-8 flex items-center justify-center z-50">
    <div class="bg-white rounded-2xl w-full max-w-3xl max-h-[80vh] flex flex-col p-5 md:p-6 shadow-2xl">
      <div class="flex items-center justify-between pb-3 border-b mb-3">
          <h3 id="progressTitle" class="text-xl font-semibold text-slate-800">Processando Arquivos...</h3>
          <button id="progressClose" class="p-1 rounded-full hover:bg-slate-200 text-slate-500 hover:text-slate-800">
              <svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M12 22C6.477 22 2 17.523 2 12S6.477 2 12 2s10 4.477 10 10-4.477 10-10 10zm0-11.414l-2.828-2.829-1.414 1.415L10.586 12l-2.828 2.828 1.414 1.415L12 13.414l2.828 2.829 1.414-1.415L13.414 12l2.828-2.828-1.414-1.415L12 10.586z"/></svg>
          </button>
      </div>
      <div id="progressContent" class="space-y-4">
        <div class="flex items-center gap-3">
          <svg id="progressSpinner" class="animate-spin h-6 w-6 text-rose-700" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" fill="none" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path></svg>
          <svg id="progressDoneIcon" class="hidden h-6 w-6 text-emerald-600" viewBox="0 0 24 24" fill="currentColor"><path d="M12 22C6.477 22 2 17.523 2 12S6.477 2 12 2s10 4.477 10 10-4.477 10-10 10zm-1.06-7.06L16.182 9.7l-1.414-1.414-5.657 5.657-2.828-2.829-1.415 1.415 4.243 4.242z"/></svg>
          <span id="progressText" class="font-medium"></span>
        </div>
        <div class="w-full bg-slate-200 rounded-full h-2.5"><div id="bar" class="bg-rose-600 h-2.5 rounded-full transition-[width] duration-200" style="width:0%"></div></div>
      </div>
      <div id="summaryContainer" class="hidden my-4"></div>
      <details id="logDetails" class="my-4">
          <summary class="cursor-pointer text-sm font-semibold text-slate-600 hover:text-slate-900">Ver logs detalhados</summary>
          <div id="logContainer" class="scrollbar-thin mt-2 p-3 bg-slate-100 rounded-lg overflow-y-auto h-56 border"></div>
      </details>
      <div id="resultContainer" class="mt-auto pt-4 border-t flex flex-col items-center gap-2"></div>
    </div>
  </div>
  
  <div id="adminModal" class="hidden fixed inset-0 p-4 flex items-center justify-center z-50">
      <div class="bg-white w-full max-w-sm rounded-2xl shadow p-5 space-y-4">
        <h2 class="text-xl font-semibold text-center">Acesso restrito</h2>
        <div class="space-y-2">
          <label class="text-sm font-medium">Senha</label>
          <input id="adminPass" type="password" class="w-full border rounded-xl px-3 py-2" placeholder="******"/>
          <p id="adminMsg" class="text-xs text-red-600 hidden">Senha inválida.</p>
        </div>
        <div class="flex justify-end gap-2">
          <button id="adminClose" class="px-3 py-2 rounded-xl hover:bg-slate-100">Fechar</button>
          <button id="adminEnter" class="px-3 py-2 rounded-2xl bg-slate-900 text-white hover:bg-slate-800">Entrar</button>
        </div>
      </div>
  </div>

<script>
const $ = (s) => document.querySelector(s);
const fileInput = $("#file"), dropZone = $("#drop"), fileHint = $("#fileHint"), cardsContainer = $("#cards");
const btnClear = $("#btnClear"), btnGo = $("#btnGo"), btnEscolher = $("#btnEscolher");
const compressMode = $("#compressMode"), selCount = $("#selCount"), selectionBox = $("#selectionBox"), actionsContainer = $("#actionsContainer");
const historyBox = $("#historyBox"), historyLinks = $("#historyLinks");
const modal = $("#modal"), modalBackdrop = $("#modalBackdrop"), modalClose = $("#modalClose"), modalTitle = $("#modalTitle"), modalFrame = $("#modalFrame");
const progressModal = $("#progressModal"), progressTitle = $("#progressTitle"), progressClose = $("#progressClose"), progressContent = $("#progressContent"), progressSpinner = $("#progressSpinner"), progressDoneIcon = $("#progressDoneIcon"), progressText = $("#progressText"), progressBar = $("#bar"), summaryContainer = $("#summaryContainer"), logDetails = $("#logDetails"), logContainer = $("#logContainer"), resultContainer = $("#resultContainer");
const btnDebug=$("#btnDebug"), adminModal=$("#adminModal");
const adminEnter=$("#adminEnter"), adminClose=$("#adminClose"), adminPass=$("#adminPass"), adminMsg=$("#adminMsg"), btnAdmin=$("#btnAdmin");

let pickedFiles = [], activeModalUrl = null;

const bytesToSize = b => b < 1024 ? b + " B" : (b < 1048576 ? (b / 1024).toFixed(1) + " KB" : (b / 1048576).toFixed(1) + " MB");

function updateSelectionUI() {
    const count = pickedFiles.length;
    selCount.textContent = `(${count})`;
    selectionBox.classList.toggle('hidden', count === 0);
    if (count === 0) {
        fileHint.textContent = "Nenhum arquivo selecionado.";
        actionsContainer.classList.add('max-h-0');
        actionsContainer.classList.remove('max-h-screen');
    } else {
        fileHint.textContent = `${count} arquivo(s) selecionado(s).`;
        actionsContainer.classList.remove('max-h-0');
        actionsContainer.classList.add('max-h-screen');
    }
}

function clearSelection() { cardsContainer.innerHTML = ""; pickedFiles = []; fileInput.value = ""; updateSelectionUI(); }
function openModal(file) { activeModalUrl = URL.createObjectURL(file); modalTitle.textContent = file.name; modalFrame.src = activeModalUrl; modal.classList.remove('hidden'); modalBackdrop.classList.remove('hidden'); }
function closeModal() { modal.classList.add('hidden'); if (activeModalUrl) { URL.revokeObjectURL(activeModalUrl); activeModalUrl = null; } modalFrame.src = 'about:blank'; if(progressModal.classList.contains('hidden') && adminModal.classList.contains('hidden')) { modalBackdrop.classList.add('hidden'); } }
function openProgressModal() { modalBackdrop.classList.remove('hidden'); progressModal.classList.remove('hidden'); }
function closeProgressModal() { modalBackdrop.classList.add('hidden'); progressModal.classList.add('hidden'); }

function addCard(file) {
    const card = document.createElement("div");
    card.className = "rounded-xl border bg-slate-100/80 p-3 flex items-center justify-between gap-3";
    card.innerHTML = `<div class="flex items-center gap-3 min-w-0"><span class="flex-shrink-0 inline-flex items-center justify-center w-8 h-8 rounded-lg bg-rose-600 text-white font-semibold text-sm">PDF</span><div class="min-w-0"><div class="font-medium text-slate-800 truncate" title="${file.name}">${file.name}</div><div class="text-xs text-slate-500">${bytesToSize(file.size)}</div></div></div><div class="flex-shrink-0 flex items-center gap-1"><button data-preview title="Visualizar" class="p-2 rounded-lg hover:bg-slate-200 text-slate-600"><svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z"/></svg></button><button data-remove title="Remover" class="p-2 rounded-lg hover:bg-rose-100 text-rose-600"><svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M7 11v2h10v-2H7zm5-9C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z"/></svg></button></div>`;
    card.querySelector("[data-preview]").onclick = () => openModal(file);
    card.querySelector("[data-remove]").onclick = () => { const idx = pickedFiles.indexOf(file); if (idx > -1) { pickedFiles.splice(idx, 1); } card.remove(); updateSelectionUI(); };
    cardsContainer.appendChild(card);
}

function handleFileSelection(fileList) {
    const newFiles = Array.from(fileList);
    clearSelection();
    pickedFiles = newFiles.filter(f => f.name.toLowerCase().endsWith('.pdf'));
    if (pickedFiles.length === 0) { fileHint.textContent = "⚠️ Nenhum arquivo PDF válido foi selecionado."; updateSelectionUI(); return; }
    pickedFiles.forEach(addCard);
    updateSelectionUI();
}

["dragenter", "dragover"].forEach(ev=> { dropZone.addEventListener(ev, e=>{ e.preventDefault(); dropZone.classList.add("bg-rose-50"); }); });
["dragleave", "drop"].forEach(ev=> { dropZone.addEventListener(ev, e=>{ e.preventDefault(); dropZone.classList.remove("bg-rose-50"); }); });
dropZone.addEventListener("drop", e => { if (e.dataTransfer?.files?.length) handleFileSelection(e.dataTransfer.files); });
btnEscolher.addEventListener("click", () => { fileInput.click(); });
fileInput.addEventListener("change", () => { if (fileInput.files.length > 0) handleFileSelection(fileInput.files); });
btnClear.onclick = clearSelection;
btnGo.onclick = () => { if (pickedFiles.length === 0) { return; } runJob(pickedFiles, false); };
modalClose.onclick = closeModal;
progressClose.onclick = closeProgressModal;
modalBackdrop.onclick = () => { closeModal(); closeProgressModal(); };
btnDebug.onclick=()=>{ modalBackdrop.classList.remove("hidden"); adminModal.classList.remove("hidden"); }
adminClose.onclick=()=>{ modalBackdrop.classList.add("hidden"); adminModal.classList.add("hidden"); adminMsg.classList.add("hidden"); adminPass.value=""; }
adminEnter.onclick=()=>{ if(adminPass.value==="123456"){ adminMsg.classList.add("hidden"); modalBackdrop.classList.add("hidden"); adminModal.classList.add("hidden"); adminPass.value=""; btnAdmin.classList.remove("hidden"); } else { adminMsg.classList.remove("hidden"); } };
btnAdmin.addEventListener("click", ()=>{ if (pickedFiles.length === 0) { alert("Selecione pelo menos um arquivo para testar."); return; } runJob(pickedFiles, true); });

async function runJob(files, metricOnly) {
    resultContainer.innerHTML = "";
    logContainer.innerHTML = "";
    summaryContainer.innerHTML = "";
    summaryContainer.classList.add("hidden");
    logDetails.open = false;
    logDetails.querySelector('summary').innerHTML = "Ver logs detalhados";
    logDetails.style.display = 'block';
    progressBar.style.width = "0%";
    progressText.textContent = "Enviando arquivos...";
    progressTitle.textContent = metricOnly ? "Testando Desempenho..." : "Processando Arquivos...";
    progressSpinner.classList.remove('hidden');
    progressDoneIcon.classList.add('hidden');
    openProgressModal();

    const fd = new FormData();
    files.forEach(f => fd.append("files", f));
    fd.append("compress_mode", compressMode.checked ? "true" : "false");
    fd.append("metric_only", metricOnly ? "true" : "false");

    const res = await fetch("/api/process", { method: "POST", body: fd });
    const { job_id, total_pages } = await res.json();
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/${job_id}`);
    let donePages = 0;

    ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        switch (msg.event) {
            case "page_done":
                donePages++;
                const percent = Math.min(100, Math.round((donePages / (total_pages || 1)) * 100));
                progressBar.style.width = percent + "%";
                progressText.textContent = metricOnly ? `Analisando… ${donePages}/${total_pages} páginas` : `Processando… ${donePages}/${total_pages} páginas (${percent}%)`;
                if (!metricOnly) {
                    const logEntry = document.createElement('p');
                    logEntry.className = "text-sm text-slate-600 border-b border-slate-200 pb-1 mb-1 font-mono";
                    logEntry.innerHTML = `Pág. <b>${msg.data.page}</b> de <i>${msg.data.file}.pdf</i>  <span class="text-slate-400"> ➤ </span> <span class="font-medium text-emerald-700">${msg.data.newName}.pdf</span>`;
                    logContainer.appendChild(logEntry);
                    logContainer.scrollTop = logContainer.scrollHeight;
                }
                break;
            case "finished":
                progressSpinner.classList.add('hidden');
                progressDoneIcon.classList.remove('hidden');
                progressTitle.textContent = "Processamento Concluído!";
                
                const summary = msg.data.summary;
                summaryContainer.innerHTML = `<div class="p-3 bg-blue-50 border border-blue-200 rounded-lg text-center text-sm">
                                        <p class="font-semibold text-blue-800">Resumo da Execução</p>
                                        <p><span class="font-medium">${summary.renamed}</span> páginas renomeadas com sucesso.</p>
                                        <p class="${summary.manual > 0 ? 'text-amber-700' : ''}"><span class="font-medium">${summary.manual}</span> páginas salvas como 'MANUAL'.</p>
                                        </div>`;
                if (summary.manual > 0) {
                    logDetails.querySelector('summary').innerHTML = `Ver logs detalhados <span class="font-semibold text-amber-700">(${summary.manual} para revisar)</span>`;
                }
                summaryContainer.classList.remove("hidden");

                resultContainer.innerHTML = "";
                historyBox.classList.remove("hidden");
                
                for (const url of msg.data.urls) {
                    const filename = url.split("/").pop();
                    const commonClasses = "inline-flex items-center justify-center gap-2 rounded-xl text-white px-4 py-2 text-base font-medium";
                    
                    const modalBtn = document.createElement("a");
                    modalBtn.className = `${commonClasses} bg-emerald-600 hover:bg-emerald-700 w-full`;
                    modalBtn.href = url;
                    modalBtn.innerHTML = `<svg class="w-6 h-6" viewBox="0 0 24 24" fill="currentColor"><path d="M12 15a1 1 0 01-.7-.29l-4-4a1 1 0 111.4-1.42L12 12.59l3.3-3.3a1 1 0 111.4 1.42l-4 4a1 1 0 01-.7.29zM12 3a9 9 0 109 9 9 9 0 00-9-9zm0 16a7 7 0 117-7 7 7 0 01-7 7z"/></svg><span>Baixar ${filename}</span>`;
                    modalBtn.onclick = closeProgressModal;
                    resultContainer.appendChild(modalBtn);
                    
                    const historyLi = document.createElement("li");
                    historyLi.className = "flex items-center justify-between gap-3 rounded-xl border bg-slate-100 px-3 py-2";
                    historyLi.innerHTML = `<div class="flex items-center gap-2 min-w-0"><svg class="w-6 h-6 text-slate-500" viewBox="0 0 24 24" fill="currentColor"><path d="M5.828 20a1 1 0 01-.707-.293l-2.828-2.828a1 1 0 111.414-1.414l2.828 2.828a1 1 0 01-.707 1.707zM16 20a1 1 0 01-.7-1.71l2.83-2.83a1 1 0 011.41 1.41l-2.83 2.83a1 1 0 01-.7.3zM9 16a1 1 0 01-1-1V5a1 1 0 012 0v10a1 1 0 01-1 1zm6 0a1 1 0 01-1-1V5a1 1 0 012 0v10a1 1 0 01-1 1zm-4-3a1 1 0 010-2h2a1 1 0 010 2h-2zm-4 3a1 1 0 01-1-1V5a1 1 0 012 0v10a1 1 0 01-1 1zm12 0a1 1 0 01-1-1V5a1 1 0 012 0v10a1 1 0 01-1 1z"/></svg><span class="truncate text-slate-800">${filename}</span></div><a class="${commonClasses} bg-slate-700 hover:bg-slate-800 text-sm !px-3 !py-2" href="${url}">Baixar</a>`;
                    historyLinks.prepend(historyLi);
                }
                ws.close();
                clearSelection();
                break;
            case "metric":
                progressSpinner.classList.add('hidden');
                progressDoneIcon.classList.remove('hidden');
                progressTitle.textContent = "Métrica de Desempenho";
                const ramUsage = msg.data.ram;
                const ramLimit = 512;
                const ramColor = ramUsage > ramLimit ? 'text-red-600 font-bold' : 'text-emerald-600 font-bold';
                const ramMessage = ramUsage > ramLimit ? `(Acima do limite de ${ramLimit}MB)` : `(Dentro do limite de ${ramLimit}MB)`;

                summaryContainer.innerHTML = `<div class="p-4 bg-slate-100 rounded-lg text-center space-y-2">
                                                <p class="text-lg">Páginas Processadas: <span class="font-semibold">${msg.data.pages}</span></p>
                                                <p class="text-lg">Tempo Total: <span class="font-semibold">${msg.data.time} segundos</span></p>
                                                <div>
                                                  <p class="text-lg">Pico de RAM: <span class="${ramColor}">${ramUsage} MB</span></p>
                                                  <p class="text-sm text-slate-500">${ramMessage}</p>
                                                </div>
                                              </div>`;
                summaryContainer.classList.remove("hidden");
                logDetails.style.display = 'none';

                const closeBtnMetric = document.createElement("button");
                closeBtnMetric.className = "mt-4 w-full text-slate-600 hover:text-slate-900 py-2";
                closeBtnMetric.textContent = "Fechar";
                closeBtnMetric.onclick = closeProgressModal;
                resultContainer.appendChild(closeBtnMetric); 
                ws.close();
                break;
            case "error":
                progressTitle.textContent = "Erro no Processamento";
                progressText.textContent = "Ocorreu um erro.";
                summaryContainer.innerHTML = `<p class="text-red-600 p-4 bg-red-50 rounded-lg">${msg.data.message}</p>`;
                summaryContainer.classList.remove("hidden");
                progressSpinner.classList.add('hidden');
                ws.close();
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
    for p in saved:
        try:
            with fitz.open(p) as d: total_pages += d.page_count
        except Exception:
            pass

    JOBS[job_id] = {
        "dir": job_dir, "in": saved, "out": out_dir,
        "compress_mode": (compress_mode.lower() == "true"),
        "metric_only": (metric_only.lower() == "true"),
        "total_pages": total_pages
    }

    if JOBS[job_id]["metric_only"]:
        asyncio.create_task(run_in_threadpool(process_metric_job, job_id))
    else:
        asyncio.create_task(run_in_threadpool(process_normal_job, job_id))

    return {"job_id": job_id, "total_pages": total_pages}

# ==== WebSocket ====
@app.websocket("/ws/{job_id}")
async def ws_progress(ws: WebSocket, job_id: str):
    await ws.accept()
    WS.setdefault(job_id, []).append(ws)
    try:
        total = JOBS.get(job_id, {}).get("total_pages", 0)
        await ws.send_text(json.dumps({"event":"hello","data":{"total_pages":total}}))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        try: WS[job_id].remove(ws)
        except Exception: pass

# Dev runner
if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)