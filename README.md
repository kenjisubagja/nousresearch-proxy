## Nousresearch-proxy
Free model from nousresearch : QWEN4-MAX

# Nous-proxy

Proxy lokal buat pakai akun Nous Portal dari tool apa pun. Satu endpoint
OpenAI-compatible di 127.0.0.1:8899, credential diurus otomatis, bisa banyak
akun sekaligus dengan round-robin. Kena rate limit atau satu akun mati,
request pindah ke akun berikutnya.

## Yang perlu ada

- Python 3.10+ (stdlib saja, nggak butuh pip)
- Hermes Agent yang sudah login Nous: `hermes auth add nous`
  (install Hermes: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`)
- Akun Nous Portal. Free tier cukup buat model `:free`

Nggak ada install buat nous-proxy sendiri: salin folder ini ke mesin
tujuan, folder auths/ dibuat otomatis pas pertama run.

## Jalanin

    python3 ~/nous-proxy/nous_proxy.py

Arahkan client ke:

    base_url  http://127.0.0.1:8899/v1
    api_key   bebas
    model     qwen/qwen4-max:free (id lain: curl /v1/models)

Tes cepat: `curl -s http://127.0.0.1:8899/health`

Kalau auths/ kosong, proxy pakai credential Hermes yang live. Kalau ada
file di auths/, file-file itu yang jadi pool.

## Tambah akun

    python3 ~/nous-proxy/nous_login.py          # auth1.json, auth2.json, ...
    python3 ~/nous-proxy/nous_login.py akunB    # nama bebas

Dia kasih URL, lu login di browser, file tersimpan sendiri di auths/.
Restart proxy setelah nambah file. Pastikan browser login sebagai akun
yang berbeda tiap file. Jangan taruh login yang sama di dua file: refresh
token Nous single-use, chain dobel bikin sesi dicabut.

## Nyambungin ke agent dan 9router

Hermes:

    hermes config set providers.nous-proxy.api http://127.0.0.1:8899/v1
    hermes config set providers.nous-proxy.transport chat_completions

lalu di dalam CLI: `/model custom:nous-proxy:qwen/qwen4-max:free --global`

OpenClaw, di ~/.openclaw/openclaw.json:

    {
      agents: { defaults: { model: { primary: "nous-proxy/qwen/qwen4-max:free" } } },
      models: { providers: { "nous-proxy": {
        baseUrl: "http://127.0.0.1:8899/v1",
        api: "openai-completions",
        models: [{ id: "qwen/qwen4-max:free", name: "Nous pool" }]
      } } }
    }

Claude Code bicara format Anthropic, jadi butuh penerjemah. Paling gampang
claude-code-router:

    npm install -g @anthropic-ai/claude-code @musistudio/claude-code-router

~/.claude-code-router/config.json:

    {
      "Providers": [{
        "name": "nous",
        "api_base_url": "http://127.0.0.1:8899/v1/chat/completions",
        "api_key": "***",
        "models": ["qwen/qwen4-max:free"],
        "transformer": { "use": ["OpenAI"] }
      }],
      "Router": { "default": "nous,qwen/qwen4-max:free" }
    }

Jalankan dengan `ccr code`.

9router (di mesin ini sudah jalan di port 20128): buka
http://localhost:20128/dashboard, login, masuk ke Providers, tambah custom
endpoint OpenAI-compatible:

    name/prefix  nous
    baseUrl      http://127.0.0.1:8899/v1
    apiType      chat
    apiKey       bebas

Modelnya nanti dipanggil `nous/<model-id>`, misal
`nous/qwen/qwen4-max:free`. Client lu tetap mengarah ke 9router
(http://localhost:20128/v1 + key dari dashboard); 9router yang meneruskan
ke proxy ini. Claude Code juga bisa lewat 9router karena dia
menerjemahkan Anthropic ke OpenAI.

Codex, di ~/.codex/config.toml:

    model = "qwen/qwen4-max:free"
    model_provider = "nouspool"

    [model_providers.nouspool]
    base_url = "http://127.0.0.1:8899/v1"
    wire_api = "chat"
    env_key = "NOUS_PROXY_KEY"

Export `NOUS_PROXY_KEY=bebas` sebelum menjalankan codex.

## Umur token

Access token umurnya sekitar satu jam, lalu di-refresh otomatis. Nggak
dipake beberapa jam atau beberapa hari tetap aman: pas dipakai lagi dia
refresh sendiri, nggak perlu login ulang. Selama proxy jalan, keepalive
me-refresh sebelum token mati walau nggak ada request (matikan dengan
KEEPALIVE=0).

Batas kerasnya: grant login mati 30 hari setelah approval, dan refresh
tidak memperpanjangnya. Ini kebijakan Portal, dikonfirmasi di
https://github.com/NousResearch/hermes-agent/issues/117461 — Hermes yang
jalan 24/7 pun logout di hari ke-30. Sisa umur per akun bisa dicek:

    curl -s http://127.0.0.1:8899/stats

`grant_days_left` itu hitung mundurnya; log ngasih warning kalau sisa 5
hari. Login ulang sebelum habis:

    python3 ~/nous-proxy/nous_login.py --force authN

Kalau grant satu akun mati, pool tetap jalan pakai akun yang lain.

## Keamanan

Proxy cuma listen di 127.0.0.1. File auth permission 600, folder 700,
token nggak pernah ditulis ke log. Refresh selalu lewat jalur resmi
Hermes, jadi nggak ada refresh liar yang memicu refresh_token_reused.
Kalau mesin dipakai rame-rame, set LOCAL_KEY=<key> dan client wajib pakai
key itu. Jangan share folder auths/ ke mana-mana.

## Env lain

PORT, UPSTREAM, AUTH_DIR, INCLUDE_MAIN=1 (ikutkan credential live ke
pool), BACKEND_MODE=auto|inprocess|cli|readonly, NO_REFRESH=1,
COOLDOWN_429=60, COOLDOWN_401=120, STRIP_RESPONSES=1, DUMP_DIR, LOG.

