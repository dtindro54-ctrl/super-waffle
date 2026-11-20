import os
import json
import random
import csv
import io
import socket
import base64
import sqlite3
import secrets
import shutil
import qrcode
from io import BytesIO
from datetime import datetime
from functools import wraps
from flask import Flask, request, render_template_string, redirect, url_for, Response, session, abort, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from pypdf import PdfReader
from google import genai
from google.genai import types

app = Flask(__name__)

# --- KONFIGURASI KEAMANAN & IDENTITAS SEKOLAH ---
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# API Key Gemini (Ganti dengan API Key Anda dari Google AI Studio)
client = genai.Client(api_key="ISI_API_KEY_GEMINI_ANDA")

# Kredensial Guru
USERNAME_GURU = "admin"
PASSWORD_HASH_GURU = generate_password_hash("guru123")

# Identitas Kop Surat Sekolah
NAMA_DINAS = "PEMERINTAH PROVINSI JAWA TENGAH / DINAS PENDIDIKAN"
NAMA_SEKOLAH = "SMA / SMK NEGERI CONTOH NUSANTARA"
ALAMAT_SEKOLAH = "Jl. Pendidikan No. 123, Semarang | Telp. (024) 1234567"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "ujian_cbt.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)

SECRET_HEADER_KEY = "EXAM-SECURE-2026"
QR_PREFIX = "EXAMSEC::"

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS ujian (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            judul TEXT NOT NULL,
            mapel TEXT NOT NULL,
            durasi_menit INTEGER NOT NULL,
            aktif INTEGER DEFAULT 0,
            token_susulan TEXT DEFAULT '',
            dibuat_pada TEXT
        )
    """)
    try:
        c.execute("ALTER TABLE ujian ADD COLUMN token_susulan TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS soal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ujian_id INTEGER NOT NULL,
            pertanyaan TEXT NOT NULL,
            opsi_a TEXT NOT NULL,
            opsi_b TEXT NOT NULL,
            opsi_c TEXT NOT NULL,
            opsi_d TEXT NOT NULL,
            jawaban_benar TEXT NOT NULL,
            FOREIGN KEY (ujian_id) REFERENCES ujian (id) ON DELETE CASCADE
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS hasil_siswa (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ujian_id INTEGER NOT NULL,
            nama TEXT NOT NULL,
            kelas TEXT NOT NULL,
            skor INTEGER NOT NULL,
            waktu_selesai TEXT NOT NULL,
            FOREIGN KEY (ujian_id) REFERENCES ujian (id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()

def buat_auto_backup():
    if os.path.exists(DB_NAME):
        tgl = datetime.now().strftime("%Y%m%d")
        tujuan = os.path.join(BACKUP_DIR, f"auto_backup_{tgl}.db")
        if not os.path.exists(tujuan):
            shutil.copy2(DB_NAME, tujuan)

init_db()
buat_auto_backup()

def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(24)
    return session["csrf_token"]

def validasi_csrf():
    token_form = request.form.get("csrf_token", "")
    if not token_form or token_form != session.get("csrf_token"):
        abort(403, description="Token keamanan tidak valid (CSRF).")

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("guru_login"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

HTML_LOGIN = """
<!DOCTYPE html>
<html>
<head>
    <title>Login Panel Guru</title>
    <style>
        body { font-family: 'Segoe UI', sans-serif; background: #e3f2fd; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .login-box { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.12); width: 330px; }
        input { width: 100%; padding: 10px; margin: 8px 0 15px; border: 1px solid #ccc; border-radius: 5px; box-sizing: border-box; }
        button { width: 100%; padding: 11px; background: #1976d2; color: white; border: none; border-radius: 5px; font-weight: bold; cursor: pointer; font-size: 15px; }
    </style>
</head>
<body>
    <div class="login-box">
        <h3 style="text-align: center; margin-top: 0;">Login Panel Guru</h3>
        {% if error %}<p style="color: red; font-size: 13px; text-align: center;">{{ error }}</p>{% endif %}
        <form method="POST">
            <label>Username:</label>
            <input type="text" name="username" required autofocus autocomplete="off">
            <label>Password:</label>
            <input type="password" name="password" required>
            <button type="submit">Masuk ke Panel</button>
        </form>
    </div>
</body>
</html>
"""

HTML_GURU = """
<!DOCTYPE html>
<html>
<head>
    <title>Panel Guru - CBT SQLite & AI Generator</title>
    <style>
        body { font-family: 'Segoe UI', sans-serif; max-width: 980px; margin: 20px auto; padding: 0 15px; background: #f4f6f8; }
        .card { background: white; border: 1px solid #ddd; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); position: relative; }
        input[type=text], input[type=number] { width: 100%; padding: 8px; margin: 5px 0 12px; border: 1px solid #ccc; border-radius: 5px; box-sizing: border-box; }
        .btn { padding: 7px 13px; background: #1976d2; color: white; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; display: inline-block; font-size: 13px; font-weight: bold; }
        .btn-green { background: #2e7d32; }
        .btn-orange { background: #ef6c00; }
        .btn-red { background: #c62828; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; position: relative; z-index: 2; }
        th, td { border: 1px solid #444; padding: 9px; text-align: left; }
        th { background: rgba(227, 242, 253, 0.85); }
        .badge-aktif { background: #2e7d32; color: white; padding: 3px 8px; border-radius: 4px; font-size: 12px; }
        .badge-nonaktif { background: #757575; color: white; padding: 3px 8px; border-radius: 4px; font-size: 12px; }
        .inline-form { display: inline; }
        .watermark-print { display: none; }

        @media print {
            .no-print { display: none !important; }
            body { background: white; margin: 0; }
            .card { border: none; box-shadow: none; padding: 10px; }
            .watermark-print {
                display: block;
                position: fixed;
                top: 52%;
                left: 50%;
                transform: translate(-50%, -50%);
                width: 380px;
                opacity: 0.12;
                z-index: 1;
                pointer-events: none;
            }
            th { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
        }
    </style>
</head>
<body>
    <div class="no-print">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
            <h2 style="margin: 0;">Panel Kontrol Guru</h2>
            <a href="/logout" class="btn btn-red">Logout</a>
        </div>

        <div class="card" style="border-left: 5px solid #2e7d32; background: #f1f8e9; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px;">
            <div>
                <b>Cadangan Database (Bank Soal & Nilai):</b>
                <p style="margin: 3px 0 0; font-size: 12px; color: #33691e;">Auto-backup harian tersimpan di folder <code>/backups</code>.</p>
            </div>
            <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                <a href="/backup_db" class="btn btn-green">Download Backup (.db)</a>
                <form method="POST" action="/restore_db" enctype="multipart/form-data" style="display: inline-flex; gap: 5px; align-items: center;" onsubmit="return confirm('Restore akan menimpa data saat ini. Lanjutkan?')">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <input type="file" name="file_db" accept=".db" required style="font-size: 12px; width: 185px;">
                    <button type="submit" class="btn btn-orange">Restore DB</button>
                </form>
            </div>
        </div>

        <div class="card">
            <h3>1. Buat Paket Ujian Baru dari eBook / PDF</h3>
            <form method="POST" action="/generate" enctype="multipart/form-data">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                Judul Ujian: <input type="text" name="judul" placeholder="Contoh: Penilaian Tengah Semester Ganjil" required>
                Mata Pelajaran: <input type="text" name="mapel" placeholder="Contoh: Informatika" required>
                Durasi (Menit): <input type="number" name="durasi" value="45" min="1" max="300" required>
                Jumlah Soal: <input type="number" name="jumlah" value="5" min="1" max="30" required>
                File PDF / eBook: <input type="file" name="file_pdf" accept=".pdf" required><br><br>
                <button type="submit" class="btn">Generate Soal Otomatis & Simpan</button>
            </form>
        </div>

        <div class="card">
            <h3>2. Daftar Paket Ujian Tersimpan</h3>
            <table>
                <tr>
                    <th>Judul Ujian</th>
                    <th>Mapel</th>
                    <th>Durasi</th>
                    <th>Status</th>
                    <th>Aksi</th>
                </tr>
                {% for u in daftar_ujian %}
                <tr>
                    <td><b>{{ u.judul }}</b></td>
                    <td>{{ u.mapel }}</td>
                    <td>{{ u.durasi_menit }} Menit</td>
                    <td>
                        {% if u.aktif == 1 %}
                            <span class="badge-aktif">AKTIF</span>
                            {% if u.token_susulan %}<span class="badge-aktif" style="background:#ef6c00;">SUSULAN</span>{% endif %}
                        {% else %}
                            <span class="badge-nonaktif">Nonaktif</span>
                        {% endif %}
                    </td>
                    <td>
                        <a href="/?pilih_id={{ u.id }}" class="btn">Kelola / Nilai</a>
                        {% if u.aktif == 0 %}
                            <form method="POST" action="/aktifkan/{{ u.id }}" class="inline-form">
                                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                                <button type="submit" class="btn btn-green">Aktifkan</button>
                            </form>
                        {% endif %}
                        <form method="POST" action="/hapus/{{ u.id }}" class="inline-form" onsubmit="return confirm('Hapus paket ujian dan semua nilainya?')">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                            <button type="submit" class="btn btn-red">Hapus</button>
                        </form>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="5" style="text-align:center;">Belum ada paket ujian di database.</td></tr>
                {% endfor %}
            </table>
        </div>
    </div>

    {% if ujian_terpilih %}
        {% if ujian_terpilih.aktif == 1 %}
        <div class="card no-print" style="background: #e8f5e9; text-align: center;">
            <h3>QR Code Ujian Aktif: {{ ujian_terpilih.judul }}</h3>
            <img src="data:image/png;base64,{{ qr_image }}" alt="QR Ujian" style="width: 240px; height: 240px; border: 8px solid white; border-radius: 10px;">
            <p style="color: #2e7d32;"><b>Scan menggunakan Aplikasi Exam Browser Android resmi</b></p>
        </div>
        {% endif %}

        <div class="card no-print" style="border-left: 5px solid #ef6c00; background: #fff8e1;">
            <h3 style="margin-top: 0; color: #e65100;">Panel Pengaturan Ujian Susulan</h3>
            <form method="POST" action="/atur_susulan/{{ ujian_terpilih.id }}" style="display: flex; gap: 10px; align-items: center;">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <input type="text" name="token_susulan" value="{{ ujian_terpilih.token_susulan or '' }}" placeholder="Contoh: SUSULAN2026 (Kosongkan untuk mode reguler)" style="margin: 0; flex: 1;">
                <button type="submit" class="btn btn-orange">Simpan Pengaturan Susulan</button>
            </form>
            {% if ujian_terpilih.token_susulan %}
                <p style="margin: 8px 0 0; color: #2e7d32; font-size: 13px;">
                    <b>Mode Susulan AKTIF</b> — Token Masuk Siswa: <span style="background: #ffe0b2; padding: 2px 8px; border-radius: 4px; font-weight: bold;">{{ ujian_terpilih.token_susulan }}</span>
                </p>
            {% endif %}
        </div>

        <div class="card">
            {% if logo_tutwuri %}
                <img src="data:image/png;base64,{{ logo_tutwuri }}" class="watermark-print" alt="Watermark Tut Wuri Handayani">
            {% endif %}

            <div style="display: flex; align-items: center; border-bottom: 3px double #000; padding-bottom: 12px; margin-bottom: 15px; position: relative; z-index: 2;">
                {% if logo_tutwuri %}
                    <img src="data:image/png;base64,{{ logo_tutwuri }}" style="width: 75px; height: 75px; margin-right: 15px;">
                {% endif %}
                <div style="text-align: center; flex: 1;">
                    <p style="margin: 0; font-size: 14px; font-weight: bold;">{{ nama_dinas }}</p>
                    <h2 style="margin: 2px 0; font-size: 20px;">{{ nama_sekolah }}</h2>
                    <p style="margin: 0; font-size: 12px;">{{ alamat_sekolah }}</p>
                </div>
            </div>

            <div style="text-align: center; margin-bottom: 15px; position: relative; z-index: 2;">
                <h3 style="margin: 0; text-decoration: underline;">DAFTAR NILAI HASIL UJIAN</h3>
                <p style="margin: 5px 0;"><b>{{ ujian_terpilih.judul }}</b> | Mata Pelajaran: <b>{{ ujian_terpilih.mapel }}</b></p>
                <p style="margin: 0; font-size: 12px;">Tanggal Cetak: {{ tanggal }}</p>
            </div>

            <div class="no-print" style="margin-bottom: 15px;">
                <button onclick="window.print()" class="btn btn-orange">Cetak Nilai / Simpan PDF</button>
                <a href="/download_nilai/{{ ujian_terpilih.id }}" class="btn">Download Excel (CSV)</a>
            </div>

            <table>
                <tr>
                    <th style="width: 40px;">No</th>
                    <th>Nama Lengkap Siswa</th>
                    <th>Kelas / NIM</th>
                    <th>Waktu Selesai</th>
                    <th style="width: 75px;">Nilai</th>
                    <th class="no-print" style="width: 120px;">Aksi Susulan</th>
                </tr>
                {% for h in daftar_nilai %}
                    <tr>
                        <td>{{ loop.index }}</td>
                        <td>{{ h.nama }}</td>
                        <td>{{ h.kelas }}</td>
                        <td>{{ h.waktu_selesai }}</td>
                        <td><b>{{ h.skor }}</b></td>
                        <td class="no-print">
                            <form method="POST" action="/reset_siswa/{{ ujian_terpilih.id }}/{{ h.id }}" class="inline-form" onsubmit="return confirm('Buka kunci & hapus nilai lama {{ h.nama }} agar dapat mengikuti ujian susulan?')">
                                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                                <button type="submit" class="btn btn-red" style="padding: 4px 8px; font-size: 12px;">Buka Kunci</button>
                            </form>
                        </td>
                    </tr>
                {% else %}
                    <tr><td colspan="6" style="text-align: center;">Belum ada siswa yang mengumpulkan jawaban.</td></tr>
                {% endfor %}
            </table>

            <div style="margin-top: 40px; display: flex; justify-content: flex-end; position: relative; z-index: 2;">
                <div style="text-align: center; width: 230px;">
                    <p style="margin-bottom: 65px;">Guru Mata Pelajaran,</p>
                    <p style="margin: 0; border-top: 1px solid #000; padding-top: 5px;"><b>( ........................................ )</b></p>
                </div>
            </div>
        </div>

        <div class="card no-print">
            <h3>Edit Soal: {{ ujian_terpilih.judul }}</h3>
            <form method="POST" action="/simpan_soal/{{ ujian_terpilih.id }}">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                {% for s in daftar_soal %}
                    <div style="background: #f9f9f9; padding: 12px; margin-bottom: 12px; border-radius: 6px; border-left: 4px solid #1976d2;">
                        <b>Soal {{ loop.index }}:</b>
                        <input type="text" name="tanya_{{ s.id }}" value="{{ s.pertanyaan }}" required>
                        <input type="text" name="opsi_a_{{ s.id }}" value="{{ s.opsi_a }}" required>
                        <input type="text" name="opsi_b_{{ s.id }}" value="{{ s.opsi_b }}" required>
                        <input type="text" name="opsi_c_{{ s.id }}" value="{{ s.opsi_c }}" required>
                        <input type="text" name="opsi_d_{{ s.id }}" value="{{ s.opsi_d }}" required>
                        <b>Kunci Jawaban:</b>
                        <input type="text" name="kunci_{{ s.id }}" value="{{ s.jawaban_benar }}" required>
                    </div>
                {% endfor %}
                <button type="submit" class="btn btn-green">Simpan Perubahan Soal</button>
            </form>
        </div>
    {% endif %}
</body>
</html>
"""

HTML_SISWA = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ ujian.judul }}</title>
    <style>
        body { font-family: sans-serif; padding: 15px; max-width: 700px; margin: auto; user-select: none; }
        .timer { position: sticky; top: 0; background: #d32f2f; color: white; padding: 10px; text-align: center; font-weight: bold; border-radius: 5px; z-index: 100; }
        .soal-box { border: 1px solid #ddd; padding: 15px; margin: 15px 0; border-radius: 8px; transition: 0.2s; }
        .soal-terkunci { background: #f1f8e9; border-color: #81c784; }
        .status-lock { font-size: 12px; color: #2e7d32; font-weight: bold; float: right; }
        input[type=text] { width: 100%; padding: 8px; margin: 5px 0 15px; box-sizing: border-box; }
        button { width: 100%; padding: 12px; background: #1976d2; color: white; border: none; border-radius: 6px; font-size: 16px; font-weight: bold; }
    </style>
</head>
<body>
    {% if pesan_blokir %}
        <div style="text-align: center; padding: 30px; background: #ffebee; border: 1px solid #ef9a9a; border-radius: 10px; margin-top: 30px;">
            <h2 style="color: #c62828;">Akses Ditolak!</h2>
            <p>{{ pesan_blokir }}</p>
        </div>
    {% elif butuh_token_susulan %}
        <div style="padding: 25px; border: 1px solid #ffb74d; background: #fff8e1; border-radius: 10px; margin-top: 30px;">
            <h3 style="margin-top: 0; color: #e65100;">Verifikasi Ujian Susulan</h3>
            <p>Ujian <b>{{ ujian.judul }}</b> sedang dalam mode susulan. Masukkan Token dari pengawas:</p>
            {% if error_token %}<p style="color: red; font-weight: bold;">{{ error_token }}</p>{% endif %}
            <form method="GET" action="/ujian">
                <input type="text" name="token" required placeholder="Masukkan Token Susulan" autocomplete="off">
                <button type="submit" style="background: #ef6c00;">Buka Lembar Soal</button>
            </form>
        </div>
    {% elif skor is not none %}
        <div style="text-align: center; padding: 30px; background: #e8f5e9; border-radius: 10px; margin-top: 30px;">
            <h2>Ujian Selesai & Terkunci!</h2>
            <p>Nama: <b>{{ nama }}</b> ({{ kelas }})</p>
            <h1 style="font-size: 52px; color: #2e7d32; margin: 10px 0;">{{ skor }}</h1>
            <p>Jawaban dan nilai Anda sudah dikunci permanen. Silakan lapor kepada pengawas.</p>
        </div>
    {% else %}
        <div class="timer" id="timerDisplay">Sisa Waktu: {{ ujian.durasi_menit }}:00</div>
        <h2>{{ ujian.judul }} ({{ ujian.mapel }})</h2>
        <p style="font-size: 13px; color: #d32f2f;"><b>Perhatian:</b> Setiap jawaban yang dipilih akan langsung dikunci dan tidak dapat diubah kembali.</p>

        <form id="formUjian" method="POST" onsubmit="return bukaSemuaKunciSebelumSubmit()">
            <input type="hidden" name="token_input" value="{{ token_aktif or '' }}">
            <div class="soal-box">
                <label>Nama Lengkap:</label>
                <label>Kelas / NIM:</label>
                <input type="text" name="kelas" required placeholder="Contoh: XII IPA 1" autocomplete="off">
            </div>

            {% for s in soal_acak %}
                <div class="soal-box" id="box_{{ s.id }}">
                    <span class="status-lock" id="badge_{{ s.id }}"></span>
                    <p><b>{{ loop.index }}. {{ s.pertanyaan }}</b></p>
                    {% for opsi in [s.opsi_a, s.opsi_b, s.opsi_c, s.opsi_d] %}
                        <label style="display: block; margin: 10px 0; cursor: pointer;">
                            <input type="radio"
                                   class="radio_{{ s.id }}"
                                   name="j_{{ s.id }}"
                                   value="{{ opsi }}"
                                   onclick="kunciJawaban(this, '{{ s.id }}')"> {{ opsi }}
                        </label>
                    {% endfor %}
                </div>
            {% endfor %}
            <button type="submit">Kirim Semua Jawaban</button>
        </form>

        <script>
            function kunciJawaban(elemen, soalId) {
                const yakin = confirm("Kunci jawaban ini? Jawaban yang sudah dipilih tidak bisa diganti.");
                if (!yakin) {
                    elemen.checked = false;
                    return;
                }
                const semuaRadio = document.querySelectorAll('.radio_' + soalId);
                semuaRadio.forEach(r => {
                    if (r !== elemen) {
                        r.disabled = true;
                    }
                });
                document.getElementById('box_' + soalId).classList.add('soal-terkunci');
                document.getElementById('badge_' + soalId).textContent = "TERKUNCI";
            }

            function bukaSemuaKunciSebelumSubmit() {
                document.querySelectorAll('input[type=radio]').forEach(r => r.disabled = false);
                return true;
            }

            let detik = {{ ujian.durasi_menit }} * 60;
            const display = document.getElementById('timerDisplay');
            const interval = setInterval(() => {
                let m = Math.floor(detik / 60);
                let s = detik % 60;
                display.textContent = `Sisa Waktu: ${m}:${s < 10 ? '0' : ''}${s}`;
                if (detik <= 0) {
                    clearInterval(interval);
                    bukaSemuaKunciSebelumSubmit();
                    document.getElementById('formUjian').submit();
                }
                detik--;
            }, 1000);
        </script>
    {% endif %}
</body>
</html>
"""

def dapatkan_ip_lokal():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def buat_qr_terenkripsi(url_ujian):
    encoded_url = base64.b64encode(url_ujian.encode("utf-8")).decode("utf-8")
    payload = f"{QR_PREFIX}{encoded_url}"
    qr = qrcode.make(payload)
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")

def ekstrak_pdf(file_stream):
    reader = PdfReader(file_stream)
    teks = ""
    for page in reader.pages[:20]:
        teks += (page.extract_text() or "") + "
"
    return teks

def dapatkan_logo_tutwuri():
    path_logo = os.path.join(BASE_DIR, "tutwuri.png")
    if os.path.exists(path_logo):
        with open(path_logo, "rb") as img_file:
            return base64.b64encode(img_file.read()).decode("utf-8")
    return ""

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        user = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        if user == USERNAME_GURU and check_password_hash(PASSWORD_HASH_GURU, pw):
            session["guru_login"] = True
            get_csrf_token()
            return redirect(url_for("index"))
        error = "Username atau Password salah!"
    return render_template_string(HTML_LOGIN, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/backup_db")
@login_required
def backup_db():
    if not os.path.exists(DB_NAME):
        abort(404)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    return send_file(DB_NAME, as_attachment=True, download_name=f"backup_cbt_sekolah_{timestamp}.db")

@app.route("/restore_db", methods=["POST"])
@login_required
def restore_db():
    validasi_csrf()
    file = request.files.get("file_db")
    if not file or not file.filename.lower().endswith(".db"):
        return "Format file harus .db!", 400
    header_file = file.read(16)
    file.seek(0)
    if header_file != b"SQLite format 3\x00":
        return "File rusak atau bukan database SQLite yang sah!", 400
    if os.path.exists(DB_NAME):
        shutil.copy2(DB_NAME, os.path.join(BACKUP_DIR, f"sebelum_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"))
    file.save(DB_NAME)
    init_db()
    return redirect(url_for("index"))

@app.route("/")
@login_required
def index():
    conn = get_db()
    daftar_ujian = conn.execute("SELECT * FROM ujian ORDER BY id DESC").fetchall()
    pilih_id = request.args.get("pilih_id")
    ujian_terpilih = None
    daftar_soal = []
    daftar_nilai = []

    if pilih_id:
        ujian_terpilih = conn.execute("SELECT * FROM ujian WHERE id = ?", (pilih_id,)).fetchone()
    else:
        ujian_terpilih = conn.execute("SELECT * FROM ujian WHERE aktif = 1 LIMIT 1").fetchone()
        if not ujian_terpilih and daftar_ujian:
            ujian_terpilih = daftar_ujian[0]

    if ujian_terpilih:
        daftar_soal = conn.execute("SELECT * FROM soal WHERE ujian_id = ?", (ujian_terpilih["id"],)).fetchall()
        daftar_nilai = conn.execute("SELECT * FROM hasil_siswa WHERE ujian_id = ? ORDER BY id DESC", (ujian_terpilih["id"],)).fetchall()
    conn.close()

    ip_lokal = dapatkan_ip_lokal()
    url_ujian = f"http://{ip_lokal}:5000/ujian"
    qr_image = buat_qr_terenkripsi(url_ujian) if (ujian_terpilih and ujian_terpilih["aktif"] == 1) else ""
    tanggal_sekarang = datetime.now().strftime("%d-%m-%Y %H:%M")

    return render_template_string(
        HTML_GURU,
        daftar_ujian=daftar_ujian,
        ujian_terpilih=ujian_terpilih,
        daftar_soal=daftar_soal,
        daftar_nilai=daftar_nilai,
        qr_image=qr_image,
        tanggal=tanggal_sekarang,
        logo_tutwuri=dapatkan_logo_tutwuri(),
        nama_dinas=NAMA_DINAS,
        nama_sekolah=NAMA_SEKOLAH,
        alamat_sekolah=ALAMAT_SEKOLAH,
        csrf_token=get_csrf_token()
    )

@app.route("/generate", methods=["POST"])
@login_required
def generate():
    validasi_csrf()
    file = request.files.get("file_pdf")
    if not file or not file.filename.lower().endswith(".pdf"):
        return "File harus berformat PDF!", 400

    jumlah = min(max(int(request.form.get("jumlah", 5)), 1), 30)
    judul = request.form.get("judul", "Ujian").strip()
    mapel = request.form.get("mapel", "-").strip()
    durasi = min(max(int(request.form.get("durasi", 30)), 1), 300)

    isi_materi = ekstrak_pdf(file)
    prompt = f"""
    Buatkan {jumlah} soal pilihan ganda berbahasa Indonesia berdasarkan materi berikut.
    Output HARUS JSON array murni dengan struktur:
    [{{"pertanyaan": "...", "pilihan": ["A. ...", "B. ...", "C. ...", "D. ..."], "jawaban_benar": "A. ..."}}]
    Materi:
    {isi_materi[:18000]}
    """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    hasil_ai = json.loads(response.text)

    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE ujian SET aktif = 0")
    c.execute(
        "INSERT INTO ujian (judul, mapel, durasi_menit, aktif, token_susulan, dibuat_pada) VALUES (?, ?, ?, 1, '', ?)",
        (judul, mapel, durasi, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    ujian_id = c.lastrowid

    for s in hasil_ai:
        opsi = s.get("pilihan", [])
        while len(opsi) < 4:
            opsi.append("-")
        c.execute(
            """INSERT INTO soal (ujian_id, pertanyaan, opsi_a, opsi_b, opsi_c, opsi_d, jawaban_benar)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ujian_id, s["pertanyaan"], opsi[0], opsi[1], opsi[2], opsi[3], s["jawaban_benar"])
        )
    conn.commit()
    conn.close()
    return redirect(url_for("index", pilih_id=ujian_id))

@app.route("/aktifkan/<int:ujian_id>", methods=["POST"])
@login_required
def aktifkan(ujian_id):
    validasi_csrf()
    conn = get_db()
    conn.execute("UPDATE ujian SET aktif = 0")
    conn.execute("UPDATE ujian SET aktif = 1 WHERE id = ?", (ujian_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("index", pilih_id=ujian_id))

@app.route("/hapus/<int:ujian_id>", methods=["POST"])
@login_required
def hapus(ujian_id):
    validasi_csrf()
    conn = get_db()
    conn.execute("DELETE FROM ujian WHERE id = ?", (ujian_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/atur_susulan/<int:ujian_id>", methods=["POST"])
@login_required
def atur_susulan(ujian_id):
    validasi_csrf()
    token = request.form.get("token_susulan", "").strip()
    conn = get_db()
    conn.execute("UPDATE ujian SET token_susulan = ? WHERE id = ?", (token, ujian_id))
    conn.commit()
    conn.close()
    return redirect(url_for("index", pilih_id=ujian_id))

@app.route("/reset_siswa/<int:ujian_id>/<int:hasil_id>", methods=["POST"])
@login_required
def reset_siswa(ujian_id, hasil_id):
    validasi_csrf()
    conn = get_db()
    conn.execute("DELETE FROM hasil_siswa WHERE id = ? AND ujian_id = ?", (hasil_id, ujian_id))
    conn.commit()
    conn.close()
    return redirect(url_for("index", pilih_id=ujian_id))

@app.route("/simpan_soal/<int:ujian_id>", methods=["POST"])
@login_required
def simpan_soal(ujian_id):
    validasi_csrf()
    conn = get_db()
    daftar_soal = conn.execute("SELECT id FROM soal WHERE ujian_id = ?", (ujian_id,)).fetchall()
    for s in daftar_soal:
        sid = s["id"]
        conn.execute(
            """UPDATE soal SET pertanyaan=?, opsi_a=?, opsi_b=?, opsi_c=?, opsi_d=?, jawaban_benar=?
               WHERE id=? AND ujian_id=?""",
            (
                request.form.get(f"tanya_{sid}"),
                request.form.get(f"opsi_a_{sid}"),
                request.form.get(f"opsi_b_{sid}"),
                request.form.get(f"opsi_c_{sid}"),
                request.form.get(f"opsi_d_{sid}"),
                request.form.get(f"kunci_{sid}"),
                sid,
                ujian_id
            )
        )
    conn.commit()
    conn.close()
    return redirect(url_for("index", pilih_id=ujian_id))

@app.route("/download_nilai/<int:ujian_id>")
@login_required
def download_nilai(ujian_id):
    conn = get_db()
    ujian = conn.execute("SELECT judul FROM ujian WHERE id = ?", (ujian_id,)).fetchone()
    if not ujian:
        conn.close()
        abort(404)
    nilai = conn.execute("SELECT * FROM hasil_siswa WHERE ujian_id = ? ORDER BY id ASC", (ujian_id,)).fetchall()
    conn.close()

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["No", "Nama Siswa", "Kelas", "Waktu Selesai", "Nilai"])
    for i, h in enumerate(nilai, start=1):
        cw.writerow([i, h["nama"], h["kelas"], h["waktu_selesai"], h["skor"]])
    nama_file = f"nilai_{ujian['judul'].replace(' ', '_')}.csv"
    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={nama_file}"}
    )

@app.route("/ujian", methods=["GET", "POST"])
def ujian():
    if request.headers.get("X-Exam-Client") != SECRET_HEADER_KEY:
        return "<h2 style='color:red;text-align:center;margin-top:50px;'>AKSES DITOLAK! Gunakan Aplikasi Exam Browser Resmi.</h2>", 403

    conn = get_db()
    ujian_aktif = conn.execute("SELECT * FROM ujian WHERE aktif = 1 LIMIT 1").fetchone()
    if not ujian_aktif:
        conn.close()
        return "<h3 style='text-align:center;margin-top:50px;'>Tidak ada ujian yang sedang aktif saat ini.</h3>"

    token_wajib = (ujian_aktif["token_susulan"] or "").strip()
    if token_wajib:
        token_user = (request.args.get("token") if request.method == "GET" else request.form.get("token_input", "")).strip()
        if token_user != token_wajib:
            conn.close()
            pesan_err = "Token Ujian Susulan salah!" if token_user else ""
            return render_template_string(
                HTML_SISWA,
                ujian=ujian_aktif,
                butuh_token_susulan=True,
                error_token=pesan_err,
                skor=None,
                pesan_blokir=None
            )

    daftar_soal = conn.execute("SELECT * FROM soal WHERE ujian_id = ?", (ujian_aktif["id"],)).fetchall()
    if not daftar_soal:
        conn.close()
        return "<h3 style='text-align:center;margin-top:50px;'>Soal ujian belum tersedia.</h3>"

    if request.method == "POST":
        nama = request.form.get("nama", "Tanpa Nama").strip()
        kelas = request.form.get("kelas", "-").strip()

        sudah_ada = conn.execute(
            "SELECT id FROM hasil_siswa WHERE ujian_id = ? AND LOWER(nama) = LOWER(?) AND LOWER(kelas) = LOWER(?)",
            (ujian_aktif["id"], nama, kelas)
        ).fetchone()

        if sudah_ada:
            conn.close()
            return render_template_string(
                HTML_SISWA,
                ujian=ujian_aktif,
                skor=None,
                butuh_token_susulan=False,
                pesan_blokir=f"Siswa atas nama {nama} ({kelas}) sudah menyelesaikan ujian ini dan data telah dikunci."
            )

        benar = 0
        total = len(daftar_soal)
        for s in daftar_soal:
            if request.form.get(f"j_{s['id']}") == s["jawaban_benar"]:
                benar += 1
        skor = round((benar / total) * 100)
        waktu_selesai = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

        conn.execute(
            "INSERT INTO hasil_siswa (ujian_id, nama, kelas, skor, waktu_selesai) VALUES (?, ?, ?, ?, ?)",
            (ujian_aktif["id"], nama, kelas, skor, waktu_selesai)
        )
        conn.commit()
        conn.close()
        return render_template_string(
            HTML_SISWA,
            ujian=ujian_aktif,
            skor=skor,
            nama=nama,
            kelas=kelas,
            butuh_token_susulan=False,
            pesan_blokir=None
        )

    conn.close()
    soal_acak = list(daftar_soal)
    random.shuffle(soal_acak)
    return render_template_string(
        HTML_SISWA,
        ujian=ujian_aktif,
        skor=None,
        soal_acak=soal_acak,
        token_aktif=token_wajib,
        butuh_token_susulan=False,
        pesan_blokir=None
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
