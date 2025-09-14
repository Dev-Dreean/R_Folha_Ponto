import os, time, psutil, threading, gc
from FolhaPonto_GUI import split_pdf_to_folder, rename_by_text  # importa suas funções

def medir(pdf):
    base_dir = os.path.dirname(pdf)
    dest = os.path.join(base_dir, "desmembrado")
    os.makedirs(dest, exist_ok=True)
    pages = split_pdf_to_folder(pdf, dest, print)
    for p in pages:
        rename_by_text(p, print)

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python medir_ram.py arquivo.pdf")
        sys.exit(1)

    proc = psutil.Process(os.getpid())
    peak = [0]

    def sampler():
        while th.is_alive():
            rss = proc.memory_info().rss
            peak[0] = max(peak[0], rss)
            time.sleep(0.05)

    th = threading.Thread(target=medir, args=(sys.argv[1],))
    mon = threading.Thread(target=sampler)
    th.start(); mon.start()
    th.join(); mon.join(); gc.collect()

    print(f"RAM pico: {peak[0]/1024/1024:.1f} MB")
