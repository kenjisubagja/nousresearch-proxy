## Nousresearch-proxy
Free model from nousresearch : QWEN4-MAX
## Penjelasan

Gateway lokal yang bikin langganan/akun **Nous Portal** (inference-api.nousresearch.com)
bisa dipakai dari tool apa pun yang bicara OpenAI-compatible API — Hermes,
OpenClaw, Claude Code (lewat penerjemah), Codex CLI, 9router, bahkan script
Python biasa. Satu endpoint lokal (`http://127.0.0.1:8899/v1`), credential
diurus otomatis.

## Cara Kerja

- **Pakai akun Nous dari mana saja** tanpa copy-paste API key. Proxy menumpang
  credential Hermes yang sudah login, atau file auth hasil `nous_login.py`.
- **Pool banyak akun sekaligus**: taruh auth1.json, auth2.json, ... di folder
  `auths/`, request dilempar **round-robin** bergiliran. Rate limit kena di
  satu akun? Request otomatis failover ke akun lain. Cocok buat nyebarin
  beban kerja berat lintas akun yang lu punya.
- **Token nggak pernah expired di tengah jalan**: refresh lewat jalur RESMI
  Hermes (fungsi yang sama dengan runtime-nya), jadi aman dari
  `refresh_token_reused` — refresh token Nous itu single-use, refresh liar
  dari script luar bisa mencabut seluruh sesi. Di sini nggak ada refresh liar.
- **Streaming SSE, /v1/models, chat completions** semua diteruskan transparan.

Cara kerja singkat:

```
client (Hermes/OpenClaw/Claude/dll)
   -> http://127.0.0.1:8899/v1  (nous_proxy.py, round-robin)
      -> auths/auth1.json / auth2.json / ... (atau credential Hermes live)
         -> https://inference-api.nousresearch.com/v1
```

## Struktur

```
nous-proxy/
  nous_proxy.py    proxy utama (pool round-robin + failover + refresh resmi)
  nous_login.py    login akun Nous ala Hermes -> simpan ke auths/<nama>.json
  auths/           taruh file credential di sini (auth1.json, auth2.json, ...)
  auths/.state/    sandbox refresh per file (dibuat otomatis, jangan diedit)
  nous_proxy.log   log request (tanpa token)
```

## Prasyarat (yang harus terinstall)

1. **Python 3.10+** — cek: `python3 --version`.
   Script ini stdlib-only, TIDAK butuh pip install apa pun.
2. **Hermes Agent** — cek: `hermes --version`.
   Dipakai untuk jalur login resmi + auto-refresh token. Login Nous-nya harus
   sudah ada: `hermes auth status nous`.
   Install: https://hermes-agent.nousresearch.com/docs
   (Tanpa Hermes proxy tetap bisa jalan mode readonly `NO_REFRESH=1`, tapi
   token mati setelah ~1 jam dan tidak diperpanjang — tidak disarankan.)
3. **Akun Nous Portal** — free tier cukup untuk model `:free`
   (mis. `qwen/qwen4-max:free`); model lain butuh subscription/credits.
   Login: https://portal.nousresearch.com
4. **curl** (buat tes cepat; biasanya sudah ada).
5. Opsional, sesuai client yang mau disambungkan:
   - **OpenClaw** terinstall (docs: https://docs.openclaw.ai)
   - **Claude Code** + **Node.js** + **claude-code-router** (lihat bagian Claude)
   - **9router** (kalau mau lewat router yang sudah ada)
   - **Codex CLI**

Sudah diuji di Kali Linux (Python 3.14). macOS harusnya jalan selama Hermes
terinstall; Windows belum diuji.

## Quick start

```bash
# 1. Jalankan proxy (biarkan hidup; pakai tmux/screen kalau mau ditinggal)
python3 ~/nous-proxy/nous_proxy.py            # port 8899
# atau: tmux new -d -s nousproxy 'python3 ~/nous-proxy/nous_proxy.py'

# 2. Tes
curl -s http://127.0.0.1:8899/health
curl -s http://127.0.0.1:8899/v1/models -H 'Authorization: Bearer bebas' | head
curl -s http://127.0.0.1:8899/v1/chat/completions \
  -H 'Authorization: Bearer bebas' -H 'Content-Type: application/json' \
  -d '{"model":"qwen/qwen4-max:free","messages":[{"role":"user","content":"halo"}],"max_tokens":200}'
```

Kalau `auths/` kosong, proxy otomatis pakai credential Hermes yang live
(`~/.hermes/auth.json`) lewat jalur resmi in-process. Begitu ada file di
`auths/`, file-file itu yang jadi pool (tambah `INCLUDE_MAIN=1` kalau mau
credential live ikut jadi anggota).

## Tambah akun (multi-auth round-robin)

### Cara gampang: login lewat script

```bash
python3 ~/nous-proxy/nous_login.py            # -> auths/auth1.json
python3 ~/nous-proxy/nous_login.py            # lagi -> auths/auth2.json (akun beda!)
python3 ~/nous-proxy/nous_login.py akunC      # -> auths/akunC.json
```

Script kasih URL → lu login/approve di browser → credential otomatis tersimpan
dengan bentuk auth store Hermes lengkap (siap di-refresh resmi oleh proxy).
Jalur login = fungsi resmi Hermes in-process (`_nous_device_code_login`),
fallback stdlib dengan protokol device-code RFC 8628 yang sama.
Tidak menyentuh `~/.hermes/auth.json`.

Penting: akun yang ke-approve mengikuti sesi browser lu di portal. Buat akun
kedua, sign-in dulu sebagai akun itu di browser (atau pakai incognito/profile
browser lain). Akun yang emailnya sama dengan file existing diblok default
(`--force` kalau memang mau login ulang akun itu — chain-nya terpisah, aman).

### Atau salin manual

Login di Hermes/profile lain (`hermes --profile akunB auth add nous`), lalu
salin `~/.hermes/profiles/akunB/auth.json` ke `auths/auth2.json`. Bisa juga
salin `~/.hermes/auth.json` dari mesin lain.

### Perilaku pool

- Request dilempar bergiliran (round-robin) ke tiap auth.
- Auth yang 401 (setelah retry) atau 429 di-cooldown (120s / 60s atau
  Retry-After), request di-failover ke auth berikutnya.
- Token expired di-refresh lewat jalur RESMI: `hermes auth refresh nous`
  di sandbox HERMES_HOME per file (`auths/.state/<nama>/`), rotation dipersist
  dan disinkronkan balik ke file-nya.
- **Restart proxy setelah menambah/menghapus file auth** (pool discan saat startup).

```
!! JANGAN taruh salinan login akun yang SAMA di dua file. Refresh token Nous
!! single-use: dua store yang me-refresh chain yang sama akan saling
!! membatalkan (refresh_token_reused -> sesi dicabut, login ulang).
!! Satu file = satu akun berbeda. (Login ulang via nous_login.py aman —
!! itu membuat chain baru, bukan menyalin chain lama.)
```

## Nyambungin ke tools

Semua client mengarah ke endpoint yang sama:

```
Base URL : http://127.0.0.1:8899/v1
API key  : bebas (proxy yang pasang credential asli; atau set LOCAL_KEY
           di proxy dan samakan key-nya di client biar nggak sembarang
           proses lokal bisa pakai)
Model    : id dari /v1/models, mis. qwen/qwen4-max:free
```

### Hermes Agent

Daftarkan proxy sebagai custom provider (perintah resmi, jangan edit
config.yaml manual):

```bash
hermes config set providers.nous-proxy.api http://127.0.0.1:8899/v1
hermes config set providers.nous-proxy.transport chat_completions
```

Lalu di dalam CLI Hermes:

```
/model custom:nous-proxy:qwen/qwen4-max:free --global
```

(`--global` = jadi default permanen; tanpa itu hanya sesi ini.)
Alternatif: `hermes model` → wizard **Custom Endpoint** → isi base URL,
key bebas, model id. Katalog model diambil otomatis dari `/v1/models` proxy.

Catatan: kalau pool masih kosong (mode live), Hermes → proxy → credential
Hermes yang sama — muter doang, nggak manfaat. Manfaatnya muncul kalau
`auths/` sudah berisi beberapa akun: Hermes ikut kena round-robin pool.

### OpenClaw

Edit `~/.openclaw/openclaw.json` (JSON5; gateway hot-reload otomatis):

```json5
{
  agents: { defaults: { model: { primary: "nous-proxy/qwen/qwen4-max:free" } } },
  models: {
    mode: "merge",
    providers: {
      "nous-proxy": {
        baseUrl: "http://127.0.0.1:8899/v1",
        apiKey: "***",          // loopback + key kosong = no-auth, boleh dihapus
        api: "openai-completions",
        models: [
          { id: "qwen/qwen4-max:free", name: "Qwen4 Max (Nous pool)" }
        ]
      }
    }
  }
}
```

Referensi model: `<provider-id>/<model-id>`. Tambah entri di `models[]`
untuk model lain yang mau dipakai. Docs resmi:
https://docs.openclaw.ai/gateway/config-tools/custom-providers

### Claude Code

Claude Code bicara format **Anthropic**, sedangkan proxy ini format
**OpenAI** — jadi butuh penerjemah di tengah. Pilih salah satu:

**Opsi A — claude-code-router (paling umum):**

```bash
npm install -g @anthropic-ai/claude-code @musistudio/claude-code-router
```

Buat `~/.claude-code-router/config.json`:

```json
{
  "Providers": [
    {
      "name": "nous",
      "api_base_url": "http://127.0.0.1:8899/v1/chat/completions",
      "api_key": "***",
      "models": ["qwen/qwen4-max:free"],
      "transformer": { "use": ["OpenAI"] }
    }
  ],
  "Router": {
    "default": "nous,qwen/qwen4-max:free"
  }
}
```

Jalankan Claude Code lewat router: `ccr code`.
(`api_base_url` harus penuh sampai `/chat/completions`.)

**Opsi B — lewat 9router (sudah terinstall di mesin ini):**
9router menerjemahkan Anthropic ↔ OpenAI. Di dashboard 9router
(http://localhost:20128/dashboard) → Providers → tambah custom
OpenAI-compatible endpoint: base URL `http://127.0.0.1:8899/v1`, key bebas,
model `qwen/qwen4-max:free`. Lalu arahkan Claude Code ke endpoint 9router
seperti biasa (lihat docs 9router bagian Claude Code).

**Opsi C — LiteLLM proxy:** jalankan LiteLLM dengan backend OpenAI-compatible
ke `http://127.0.0.1:8899/v1`, lalu set `ANTHROPIC_BASE_URL` Claude Code ke
LiteLLM. Lebih berat; pakai A atau B dulu.

### Codex CLI

`~/.codex/config.toml`:

```toml
model = "qwen/qwen4-max:free"
model_provider = "nouspool"

[model_providers.nouspool]
name = "Nous Pool"
base_url = "http://127.0.0.1:8899/v1"
wire_api = "chat"
env_key = "NOUS_PROXY_KEY"
```

```bash
export NOUS_PROXY_KEY=bebas
codex
```

### 9router / client OpenAI-compatible lain

Tambah custom provider/OpenAI-compatible endpoint di tool masing-masing:
base URL `http://127.0.0.1:8899/v1`, API key bebas, model id dari `/v1/models`.

## Endpoint lokal proxy

```
/health        status + daftar member (backend, expiry token)
/stats         req/ok/gagal per member, cooldown, pointer round-robin
/v1/models     katalog model dari Nous (diteruskan, pakai auth member)
/v1/chat/completions, /v1/completions, /v1/embeddings  (diteruskan)
```

## Env proxy

```
PORT=8899            port listen (atau argumen pertama)
UPSTREAM             default https://inference-api.nousresearch.com
AUTH_DIR             folder file auth (default <folder script>/auths)
INCLUDE_MAIN=1       sertakan credential Hermes live sebagai anggota pool
BACKEND_MODE         auto (default) | inprocess | cli | readonly (member live)
NO_REFRESH=1         semua member readonly, tanpa refresh
LOCAL_KEY=<key>      client wajib pakai bearer ini
FORCE_REFRESH=1      paksa refresh member live saat startup (buat tes)
STRIP_RESPONSES=1    strip state /responses ala codex_strip_proxy (default OFF)
DUMP_DIR=<path>      simpan body request (hati-hati: isi percakapan)
COOLDOWN_429=60      detik cooldown saat rate limit (hormati Retry-After)
COOLDOWN_401=120     detik cooldown saat auth gagal
LOG=<path>           default ~/nous-proxy/nous_proxy.log
HERMES_REPO          lokasi source hermes-agent (default ~/.hermes/hermes-agent)
```

## Keamanan & catatan penting

- Proxy bind **hanya 127.0.0.1** — tidak terbuka ke jaringan.
- File auth berisi credential: folder `auths/` di-set 700, file 600.
  Jangan commit/share folder ini. Token tidak pernah ditulis ke log.
- Tanpa `LOCAL_KEY`, proses lokal apa pun bisa pakai proxy. Set `LOCAL_KEY`
  kalau mesin dipakai bersama.
- Refresh token Nous **single-use**: hanya jalur resmi yang boleh refresh
  (proxy ini sudah pakai itu). Jangan bikin script lain yang POST ke
  `/api/oauth/token` pakai token dari file-file ini.
- Satu file = satu akun berbeda. Jangan duplikat chain yang sama.
- Pool multi-akun = akun-akun milik lu sendiri. Jangan pakai credential
  orang lain.

## Troubleshooting

- **401 dari proxy** → token member expired dan refresh gagal. Cek
  `/stats`; login ulang akun itu (`nous_login.py --force` atau
  `hermes auth refresh nous` untuk member live).
- **503 "semua member gagal"** → semua auth kena cooldown/error. Cek
  `nous_proxy.log` dan `/stats`; biasanya subscription habis atau semua
  token mati.
- **429 terus di satu akun** → normal, itu rate limit; pool akan oper ke
  akun lain. Kalau semua akun kena, kurangi laju request.
- **Model tidak ditemukan** → cek id persis di `/v1/models` (case-sensitive,
  termasuk suffix `:free`).
- **Habis tambah file auth tapi nggak kepakai** → restart proxy (pool
  discan saat startup).
- **`nous_login.py` gagal import Hermes** → otomatis fallback stdlib;
  pastikan `HERMES_REPO` benar kalau instalasi Hermes di lokasi lain.

Alternatif bawaan Hermes untuk single-auth (tanpa pool):
`hermes proxy start --provider nous --port 8645`.
