# Menyalakan MadMac 24/7 (tanpa coding)

Waktu total ±60 menit. Anda hanya **menyalin perintah** di bawah ini, tanpa menulis kode.
Bot berjalan di server sewaan (VPS), jadi tetap jalan walaupun laptop Anda mati.

---

## Langkah 1: API key Testnet (sudah dilakukan di START HERE)

Simpan **API Key** dan **Secret** dari https://testnet.binancefuture.com.

## Langkah 2: Sewa server (VPS)

- Pilih penyedia VPS mana saja. Spesifikasi: **Ubuntu 24.04, 1 CPU, 1 GB RAM**.
  *(Estimasi biaya ±$5–6/bulan, cek harga penyedia.)*
- **Lokasi server: JANGAN di Amerika Serikat.** Binance memblokir IP AS.
  Pilih Singapura, Tokyo, Frankfurt, atau Jakarta. Langkah 7 (`selfcheck`) akan memastikan Binance bisa dijangkau.
- Catat **alamat IP** server. Nanti dipakai untuk mengunci API key live.

## Langkah 3: Masuk ke server dan pasang Docker

Buka **console / terminal web** dari dashboard penyedia VPS, lalu tempel:

```bash
curl -fsSL https://get.docker.com | sh
```

## Langkah 4: Bot Telegram Anda

1. Di Telegram, chat **@BotFather** → kirim `/newbot` → beri nama → salin **token**.
2. Buka bot baru Anda, tekan **Start**, kirim pesan apa saja.
3. Chat **@userinfobot** → salin angka **Id** Anda (itu `TELEGRAM_CHAT_ID`).

## Langkah 5: Ambil kode bot

```bash
git clone -b claude/binance-auto-trading-bot-f8j4zy https://github.com/masrachmad-ai/MadMac.git madmac
cd madmac
```

*Jika repo Anda private, GitHub akan meminta login. Buat "fine-grained token" read-only di
GitHub → Settings → Developer settings, lalu pakai sebagai password.*

## Langkah 6: Masukkan kunci (hanya di server)

```bash
cp .env.example .env
nano .env
```

Isi 4 baris: `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. Simpan dengan `Ctrl+O`, `Enter`, lalu `Ctrl+X`.
Biarkan baris LIVE **kosong**.

## Langkah 7: Uji sebelum jalan

```bash
docker compose build
docker compose run --rm bot python -m pytest -q          # 31 stress test → harus "31 passed"
docker compose run --rm bot python -m madmac selfcheck   # koneksi, saldo, stop-loss uji, Telegram
docker compose run --rm bot python -m madmac lab         # backtest 6 bulan data asli (±5–15 menit)
```

- `selfcheck` harus diakhiri **SEMUA OK**. Jika ada ❌, kirim **teks error-nya** ke saya (bukan key Anda).
- `lab` mencetak laporan. Strategi bertanda **❌ TIDAK LULUS** tidak akan pernah dipakai bot.
  Jika **tidak ada yang lulus**, bot tetap jalan tapi **hanya memantau**. Itu hasil yang jujur, bukan error.

## Langkah 8: Nyalakan 24/7

```bash
docker compose up -d
```

Selesai. Anda akan menerima pesan "🤖 MadMac aktif" di Telegram.

| Perlu apa | Perintah |
|---|---|
| Lihat log | `docker compose logs -f --tail 100` |
| Setelah mengubah `config.yaml` | `docker compose restart` |
| Matikan bot | `docker compose down` |
| Update kode | `git pull && docker compose up -d --build` |
| Uji ulang Lab (wajib ≤ 30 hari sekali) | `docker compose run --rm bot python -m madmac lab && docker compose restart` |

Bot **restart sendiri** jika crash, jika server reboot, atau jika macet lebih dari 3 menit.

---

## Nanti: pindah ke uang asli (JANGAN sekarang)

Hanya setelah **semua** poin di [FINAL_CHECK.md](FINAL_CHECK.md) terjawab "ya", termasuk 2 minggu paper trading.

1. Binance → API Management → buat key baru:
   ✅ **Enable Futures** · ❌ **Enable Withdrawals (jangan dicentang)** · ✅ **Restrict access to trusted IPs** → isi IP server.
2. Isi `BINANCE_LIVE_API_KEY` / `SECRET` di `.env` di server.
3. Ubah `mode: live` di `config.yaml`, lalu `docker compose restart`.
4. Ketik **`go live`** di Telegram. Tanpa langkah ini, Shield menolak semua trade live.
