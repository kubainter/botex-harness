# BoteX on Oracle Cloud Always Free (ARM64)

Systemd deployment package for a `VM.Standard.A1.Flex` instance
(4 OCPU Ampere A1, 24 GiB RAM, Ubuntu 24.04 aarch64).

## 🚀 Quickstart: One-Click Installation

To automatically install, configure and launch BoteX with all services on a clean Ubuntu VPS:

```bash
# From repository root directory on the VPS:
sudo bash deploy/oracle-vps/install.sh

# Or to also compile llama.cpp and download Bielik-11B-v3.0 Q8_0 (~11.05 GiB):
sudo bash deploy/oracle-vps/install.sh --with-bielik
```

The script will automatically configure dependencies, Python venv, user permissions, UFW firewall (port 8000), systemd services, and verify health.

---

## Layout

| File | Install to | Purpose |
|---|---|---|
| `install.sh` | CLI runner | One-click automated production installer |
| `botex-cloud.service` | `/etc/systemd/system/` | Uvicorn `botex.server_cloud:app`, 1 worker, `0.0.0.0:8000`, pinned to OCPU 0 |
| `bielik.service` | `/etc/systemd/system/` | `llama-server` for Bielik-11B-v3.0 Q8_0, `127.0.0.1:8080`, `-c 8192`, OCPUs 1-3 at idle priority |
| `botex-maintenance.service` | `/etc/systemd/system/` | `oneshot` background maintenance: SQLite WAL checkpoints, Obsidian graph indexing, log cleanup |
| `botex-maintenance.timer` | `/etc/systemd/system/` | Periodic trigger: runs 5 min after boot, then every 30 minutes |
| `botex-maintenance.sh` | `/usr/local/sbin/` (chmod 755) | Maintenance runner: index synchronization, WAL truncation, graph audit |
| `botex-nightly-curator.service` | `/etc/systemd/system/` | `oneshot` Nightly Knowledge Graph Curator & Memory Vault Compactor (Bielik LLM) |
| `botex-nightly-curator.timer` | `/etc/systemd/system/` | Daily trigger: runs at 03:30 AM with concurrency collision guard |
| `botex-curator.sh` | `/usr/local/sbin/` (chmod 755) | Curator runner: topology enrichment, orphan elimination, persistent audit trail |

CPU partitioning: OCPU 0 = BoteX, OCPUs 1-3 = Bielik, maintenance runs at `SCHED_IDLE` / nice 19 so it never competes with live agent workloads.
RAM budget: BoteX <= 6 GiB (cap), Bielik <= 16 GiB (cap, ~13 GiB typical in 8-bit precision), maintenance <= 4 GiB (cap).

## 1. Prepare the system

```bash
sudo apt update && sudo apt install -y python3-venv python3-dev build-essential cmake git curl
sudo useradd --system --home-dir /opt/botex --shell /usr/sbin/nologin botex
sudo mkdir -p /opt/botex /opt/bielik/models /var/log/botex
sudo chown -R botex:botex /opt/botex /opt/bielik /var/log/botex
```

## 2. Install BoteX

```bash
sudo -u botex git clone <your-repo-url> /opt/botex        # or rsync the tree
cd /opt/botex
sudo -u botex python3 -m venv .venv
sudo -u botex .venv/bin/pip install -r requirements.txt   # or: pip install -e .
```

Create `/opt/botex/.env` (mode 600, owner botex) with at least:

```ini
OPENROUTER_API_KEY=sk-or-...
NVIDIA_API_KEY=nvapi-...
BOTEX_ACCESS_TOKEN=<long-random-token>   # enables Strict Cloud bearer auth
```

Never commit `.env`; it is loaded automatically by BoteX and by
`EnvironmentFile=-/opt/botex/.env` in the unit.

## 3. Install llama.cpp + Bielik model

```bash
sudo git clone https://github.com/ggml-org/llama.cpp /opt/llama.cpp
cd /opt/llama.cpp
sudo cmake -B build
sudo cmake --build build --config Release -j4        # ARM NEON auto-detected
sudo chown -R botex:botex /opt/llama.cpp

# Bielik-11B-v3.0-Instruct GGUF, quantization Q8_0 (~11.05 GiB, 8-bit studio precision):
sudo -u botex curl -fL -o /opt/bielik/models/bielik-11b-v3.0-instruct.Q8_0.gguf \
  https://huggingface.co/speakleash/Bielik-11B-v3.0-Instruct-GGUF/resolve/main/Bielik-11B-v3.0-Instruct.Q8_0.gguf
```

If the GGUF filename differs, fix `BIELIK_MODEL` in `bielik.service`
(or drop an override into `/etc/systemd/system/bielik.service.d/`).
A distro `llama.cpp` package works too - point `BIELIK_BIN` at
`/usr/bin/llama-server`.

## 4. Install and enable the units

```bash
sudo cp deploy/oracle-vps/*.service deploy/oracle-vps/*.timer /etc/systemd/system/
sudo install -m 0755 deploy/oracle-vps/botex-maintenance.sh /usr/local/sbin/
sudo systemctl daemon-reload

sudo systemctl enable --now botex-cloud.service
sudo systemctl enable --now bielik.service
sudo systemctl enable --now botex-maintenance.timer
```

## 4.5. Skills Catalog & Autonomous Triage Setup (Pre-Deploy Step)

> [!NOTE]
> BoteX na VPS pełni rolę autonomicznego workera procedur inżynieryjnych. Nie wymaga ręcznego wskazywania skilli przez agentów zewnętrznych — selekcja odbywa się automatycznie na poziomie bramki L0 Triage.

1. **Struktura katalogu skilli na serwerze:**
   - Katalog: `/opt/botex/skills/` (lub ścieżka zdefiniowana w `botex.config.json` pod `paths.skills_dir`).
   - Wrzucamy tam standardowe skille inżynieryjne w formacie `SKILL.md` (np. `code-review`, `security-audit`, `refactoring`, `api-design`, `test-generation`, `knowledge-synthesis`).
2. **Autonomiczny Triage i Administracja przez Bielika (`bielik.service` / loopback:8080):**
   - Po odebraniu zadania przez MCP (`run_subagent` / `start_task`) moduł `botex/skills.py` (`triage_skills_for_task`) dopasowuje intencję zadania do zainstalowanych skilli w `/opt/botex/skills/`.
   - Wybrany skill (`SKILL.md`) jest dynamicznie wstrzykiwany jako instrukcja proceduralna do kontekstu modelu wykonawczego, a wynik zwraca listę `applied_skills`.
   - **Wytyczne dla Bielika w roli Administratora Vaulta (`botex/chat.py`):**
     - Bielik działa w reżimie **Single-Shot / High-Level Semantic Tools** (`save_memory`, `patch_memory_section`, `delete_memory`, `find_graph_health`, `list_skills`, `install_skill`) zamiast wieloetapowych diffów plikowych (`apply_patch`).
     - Obowiązuje twardy zakaz symulowania wywołań narzędzi w tekście (Zero Fast-Forwarding) oraz zakaz zapisywania tymczasowych raportów telemetrycznych/kuratorskich do `.botex/memory/` (trafiają do `.botex/analytics_digest.json` i `.botex/curator_audit_latest.json`).
     - W razie potrzeby odciążenia lokalnego CPU przy złożonych zadaniach wieloetapowych jako bezpłatny fallback w konfiguracji służy provider `nvidia` (np. `nvidia/nemotron-3-ultra-550b-a55b`).
3. **Zwrot metadanych do wywołującego agenta:**
   - BoteX w odpowiedzi JSON zwraca listę `applied_skills: [...]`, co pozwala agentom lokalnym (Antigravity, Devin) zarejestrować wykonane procedury bez marnowania tokenów na duplikację audytu.

## 5. Open the firewall

Bielik stays on loopback; only the gateway needs ingress:

```bash
sudo ufw allow 8000/tcp
```

Also allow TCP 8000 in the OCI Security List / NSG of the subnet.
All `/api/*` and `/mcp/*` endpoints then require
`Authorization: Bearer $BOTEX_ACCESS_TOKEN` (`/api/health` and the PWA shell
stay public by design).

## 6. Verify

```bash
curl -s http://127.0.0.1:8000/api/health          # {"status":"healthy",...}
curl -s http://127.0.0.1:8080/health              # llama-server ok
systemctl status botex-cloud bielik --no-pager
systemctl list-timers botex-maintenance.timer
journalctl -u botex-maintenance -n 20 --no-pager   # maintenance logs
```
