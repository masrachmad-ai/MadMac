# Strategi MadMac: penjelasan tanpa coding

Semua strategi di bawah adalah **kandidat**, bukan jaminan. Setiap pasangan strategi × koin
(6 strategi × 10 koin = 60 pasangan) harus lulus Lab dulu, dan yang tidak lulus tidak akan pernah di-trade.
Semua strategi hanya memakai candle yang **sudah tutup**. Ada tes otomatis yang membuktikan bahwa sinyal
di candle mana pun tidak berubah ketika data masa depan ditambahkan, jadi tidak ada "ramalan" dengan
melihat ke depan.

## Smart money

### `smc_sweep`: sapu likuiditas
- **Idenya:** stop-loss trader ritel menumpuk tepat di bawah *swing low* terakhir. Pemain besar sering
  mendorong harga menembus level itu untuk "menyapu" stop tersebut, lalu harga kembali naik.
- **Aturan LONG:** low candle menembus swing low terakhir (minimal 0,1×ATR), lalu candle **ditutup kembali
  di atasnya**. Candle itu hijau, pembeli agresif (taker buy) > 50%, dan **open interest turun** selama
  sapuan (tanda stop/likuidasi tersapu). Funding tidak boleh terlalu positif (pasar belum terlalu ramai long).
- **Stop:** di bawah ujung sapuan (− 0,2×ATR). **Target:** 2R. **Batas waktu:** 36 candle (3 jam).
- SHORT adalah cerminannya, di atas swing high.

### `fvg_retest`: fair value gap
- **Idenya:** gerakan sangat cepat meninggalkan "celah" (gap) antara candle 1 dan candle 3. Harga sering
  kembali sekali untuk mengisi celah itu sebelum melanjutkan tren.
- **Aturan LONG:** ada gap bullish (low candle 3 > high candle 1, minimal 0,3×ATR) dalam tren naik (> EMA200).
  Saat harga **pertama kali** kembali masuk ke gap lalu ditutup hijau di atas dasar gap, bot masuk.
- **Stop:** di bawah dasar gap. **Target:** 2R.

## Derivatif

### `bos_oi`: break of structure + uang baru
- **Idenya:** breakout yang didukung **posisi baru** (open interest naik) dan pembeli agresif lebih bisa
  dipercaya daripada breakout yang hanya berasal dari short yang menutup posisi.
- **Aturan LONG:** candle ditutup menembus swing high terakhir, open interest naik ≥ 0,5% dalam 6 candle,
  dan taker buy ≥ 55%.
- **Stop:** 1×ATR. **Target:** 1,5R.

## Klasik (pembanding)

| Strategi | Ringkas |
|---|---|
| `ema_pullback` | Tren naik, harga turun ke EMA21 lalu ditutup lagi di atas EMA9 |
| `rsi_reversion` | Tren naik, RSI sempat < 30 lalu berbalik |
| `range_breakout` | Tembus high/low 20 candle dengan volume ≥ 1,5× rata-rata |

## Data yang dipakai

| Data | Backtest (Lab) | Live (Scout) |
|---|---|---|
| Harga, volume, **taker buy volume** | Arsip resmi `data.binance.vision`, klines 5m | Binance API, 1.000 candle terakhir |
| **Open interest** | Arsip `metrics` harian (per 5 menit), dipakai **terlambat 1 periode** | `openInterestHist` 5m, dengan keterlambatan yang sama |
| **Funding rate** | Arsip `fundingRate` bulanan | `premiumIndex` (tarif saat ini) |

Ketersediaan file arsip `metrics` dan `fundingRate` belum bisa saya verifikasi dari lingkungan build.
Kalau datanya tidak ada, Lab menulis **"data open interest tidak tersedia"**, dan strategi yang
membutuhkannya tidak lulus. Data tidak pernah dikarang.

## Kenapa "smart money" belum tentu menang

Konsep SMC populer di kalangan trader ritel, dan justru karena populer, setup-nya bisa ramai dan
mudah ditebak. Lab memperlakukannya sama seperti strategi lain: **buktikan dulu di data asli
setelah fee, dan harus lebih baik dari entry acak.**
