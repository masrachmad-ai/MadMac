# Final Check: sebelum uang asli

Jawab **YA** untuk semua poin. Satu "tidak" berarti tetap di testnet.

| Pertanyaan | Bagaimana mengeceknya | Status saat build |
|---|---|---|
| Apakah setiap agent hanya punya satu tugas? | [AGENTS.md](AGENTS.md) | ✅ dibangun & diuji |
| Bisakah bot trading di luar aturan Anda? | Anda memberi izin **auto**. Bot tetap hanya trading jika: pasangan lulus Lab, Shield YA, tidak ada rem, dan (untuk live) sudah "go live". | ✅ diuji |
| Apakah bot berhenti saat data mati? | Tes #4 & #5 di [STRESS_TEST.md](STRESS_TEST.md) | ✅ data palsu · ⏳ lihat sekali di testnet |
| Apakah bot berhenti saat batas harian kena? | Tes rem harian & rugi beruntun | ✅ diuji |
| Apakah ada pasangan yang lulus Lab dengan data asli? | `data/lab_report.md` | ⏳ **belum: jalankan di server** |
| Apakah setiap pasangan lolos **2 minggu paper trading** setelah fee? | `/week` dua kali, bandingkan dengan Lab | ⏳ **belum** |
| Apakah stop-loss & limit order benar-benar jalan di Binance? | `selfcheck` + lihat order di app Binance | ⏳ **belum: perlu server Anda** |
| Apakah Anda paham setiap pesan tanpa coding? | Baca 3 laporan harian pertama | ⏳ Anda yang menilai |
| API key live: hanya Futures, withdraw MATI, dikunci ke IP server, **sub-account khusus bot**? | Binance → API Management | ⏳ nanti |

---

## ⚠️ WHY THIS COULD FAIL (dan perbaikan untuk setiap risiko)

| # | Risiko | Kenapa bisa terjadi | Perbaikan |
|---|---|---|---|
| 1 | **Tidak ada pasangan yang lulus Lab** | Edge di 5 menit setelah fee itu langka. SMC populer, jadi setup-nya ramai. | Itu hasil yang **benar**: bot hanya memantau, uang aman. Langkah berikutnya: coba 15m, atau ubah/ tambah aturan, lalu uji ulang. |
| 2 | **Lulus Lab karena kebetulan** | 60 pasangan diuji, jadi beberapa pasti terlihat bagus karena hoki. | Koreksi Benjamini-Hochberg, wajib lebih baik dari entry acak, walk-forward 6 periode, uji parameter tetangga, ditambah 2 minggu paper. |
| 3 | **Adverse selection limit order** | Limit order cenderung terisi tepat saat harga bergerak **melawan** Anda. Model Lab (harus "tembus", bukan "sentuh") hanya pendekatan. | Bandingkan rasio terisi dan P&L asli di `/week` dengan Lab. Jika jauh lebih buruk, ubah `entry_order: taker` dan uji ulang Lab. |
| 4 | **Testnet ≠ pasar asli** | Order book testnet tipis dan berbeda, jadi pengisian limit di testnet **tidak realistis**. | Anggap testnet sebagai uji mesin (order, stop, rem), **bukan** bukti profit. Bukti profit = Lab + live kecil yang diawasi. |
| 5 | **Semua koin jatuh bersamaan** | Altcoin sangat berkorelasi dengan BTC. | Maks 3 posisi, maks 2 searah, total risiko ≤ 1,5%, rem harian −3%. |
| 6 | **Fee & slippage asli > estimasi** | Angka di config adalah ESTIMASI. | Journal mencatat fee **asli** dari Binance. Sesuaikan config setelah 2 minggu, lalu uji ulang Lab. |
| 7 | **API Binance berubah (stop-loss, open interest)** | Endpoint belum bisa diverifikasi dari lingkungan build. | `selfcheck` menguji semua; Pilot selalu membaca ulang stop; jika tidak ada → tutup + rem. |
| 8 | **Data open interest/funding tidak tersedia** | Arsip mungkin berbeda format atau tidak lengkap. | Lab melaporkan "tidak tersedia"; strategi itu tidak dipakai; strategi lain tetap jalan. |
| 9 | **Rezim pasar berubah** | Pola 6 bulan lalu bisa berhenti bekerja. | Lab wajib diulang ≤ 30 hari (Shield menolak hasil lama); laporan harian memperingatkan jika hasil jauh di bawah Lab. |
| 10 | **Posisi manual Anda di akun yang sama** | Bot melihat posisi asing → rem. | Pakai **sub-account** khusus bot. |
| 11 | **Server / internet mati** | VPS bisa down. | Stop & target hidup di server Binance; bot restart sendiri. |
| 12 | **Kunci API bocor** | Ditempel di chat/screenshot. | Key hanya di `.env` server; withdraw MATI; kunci IP. |
| 13 | **Godaan menurunkan timeframe ke 1 menit** | Pada 1m, fee ≈ seluruh jarak stop. | Penjaga fee memblokir; jika tetap ingin, uji dulu di Lab (`timeframe: 1m`). |
