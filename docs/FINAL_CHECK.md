# Final Check: sebelum uang asli

Jawab **YA** untuk semua poin. Satu "tidak" berarti tetap di testnet.

| Pertanyaan | Bagaimana mengeceknya | Status saat build |
|---|---|---|
| Apakah setiap agent hanya punya satu tugas? | [AGENTS.md](AGENTS.md): Scout tidak bisa order, Shield tidak bisa order, Pilot tidak bisa menyetujui diri sendiri | ✅ dibangun & diuji |
| Bisakah bot trading tanpa persetujuan Anda? | Hanya jika Anda sendiri mengubah `approval: auto`. Tes: `test_pilot_refuses_without_shield_or_human`, `test_full_flow_signal_needs_your_tap` | ✅ tidak bisa (default) |
| Apakah bot berhenti saat data mati? | Tes #4 & #5 di [STRESS_TEST.md](STRESS_TEST.md) | ✅ diuji dengan data palsu · ⏳ perlu dilihat sekali di testnet |
| Apakah bot berhenti saat batas harian kena? | `test_daily_limit_resets_at_utc_midnight`, `test_three_losses_halt_until_resume` | ✅ diuji |
| Apakah setiap aturan lolos **2 minggu paper trading** setelah fee? | `/week` di Telegram dua kali; bandingkan dengan laporan Lab | ⏳ **belum: baru mulai** |
| Apakah stop-loss benar-benar terpasang di Binance? | `selfcheck` + lihat order terbuka di app Binance saat trade testnet pertama | ⏳ **belum: perlu server Anda** |
| Apakah Anda paham setiap pesan tanpa coding? | Baca 3 laporan harian pertama. Ada yang membingungkan? Beri tahu saya. | ⏳ Anda yang menilai |
| API key live: hanya Futures, withdraw MATI, dikunci ke IP server? | Binance → API Management | ⏳ nanti |

---

## ⚠️ WHY THIS COULD FAIL (dan perbaikan untuk setiap risiko)

| # | Risiko | Kenapa bisa terjadi | Perbaikan |
|---|---|---|---|
| 1 | **Pengaturan risiko Anda terlalu agresif** | Anda memilih 2% per trade dan leverage 20x. Dengan batas harian −3%, dua kali rugi saja sudah menghentikan bot hari itu. Lima kali rugi beruntun (normal untuk scalping) = −10% modal. | **Saran saya: `risk_per_trade_pct: 0.5`, `max_leverage: 10`.** Penjaga fee sudah memblokir stop yang terlalu dekat. Keputusan tetap di tangan Anda (`config.yaml`). |
| 2 | **Tidak ada strategi yang lulus Lab** | Scalping BTC setelah fee memang sulit. Tiga aturan bawaan adalah pola umum, bukan rahasia. | Itu hasil yang **benar**: bot hanya memantau, dan Anda tidak rugi apa-apa. Langkah berikutnya: coba timeframe 15m (fee relatif lebih kecil) atau tulis aturan Anda sendiri, lalu uji ulang di Lab. |
| 3 | **Lab lulus tapi live rugi (overfitting / rezim pasar berubah)** | Masa lalu tidak sama dengan masa depan. | Wajib out-of-sample + 2 minggu paper; Lab wajib diulang ≤30 hari sekali (Shield menolak hasil lama); bandingkan `/week` dengan Lab, dan matikan strategi yang menyimpang jauh. |
| 4 | **Fee & slippage asli lebih besar dari estimasi** | `taker_fee_pct 0.05` dan `slippage_pct 0.02` adalah **estimasi**. | Cek tarif di akun Binance Anda dan ubah `config.yaml`. Journal mencatat fee **asli** dari Binance, jadi bandingkan setelah 2 minggu. |
| 5 | **API Binance berubah (khususnya stop-loss)** | Binance memindahkan order stop ke endpoint "Algo Order" pada akhir 2025. Kode belum bisa diverifikasi ke Binance asli dari lingkungan build. | Pilot mencoba endpoint baru dulu, lalu endpoint lama; **selalu membaca ulang** bahwa stop ada; jika tidak ada → posisi ditutup + rem. `selfcheck` menguji ini sebelum bot jalan. |
| 6 | **Anda tidak sempat menekan ✅ dalam 90 detik** | Scalping butuh respons cepat, padahal tujuan Anda mengurangi waktu layar. | Konflik ini nyata. Opsi: perpanjang `approval_timeout_seconds` (setup jadi basi, dan Shield menolak jika harga sudah lari), atau setelah 2 minggu terbukti, Anda **sendiri** memutuskan `approval: auto`. |
| 7 | **Server / internet mati saat ada posisi** | VPS bisa down. | Stop-loss & target hidup **di server Binance**, bukan di bot, jadi tetap jalan walaupun bot mati. |
| 8 | **Kunci API bocor** | Key ditempel di chat, screenshot, atau repo. | Key hanya di `.env` di server (sudah di `.gitignore`); key live: withdraw MATI + kunci IP. Jika curiga bocor: hapus key di Binance segera. |
| 9 | **Likuidasi sebelum stop** | Leverage tinggi + lonjakan harga ekstrem / gap. | Margin ISOLATED; Shield menolak jika stop terlalu dekat ke estimasi likuidasi; stop memakai harga mark. Pada kejadian ekstrem, slippage tetap mungkin terjadi, dan itu **tidak bisa dihilangkan 100%**. |
| 10 | **Laporan menunjukkan untung kecil, Anda menaikkan risiko** | Bias manusia setelah beberapa kali menang. | Review mingguan mengingatkan bahwa < 30 trade belum berarti apa-apa. Naikkan risiko hanya setelah ≥ 100 trade live. |
