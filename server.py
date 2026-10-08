# -*- coding: utf-8 -*-
"""
Servidor da Pizzaria Pizzaí 🍕
- Delivery:  http://localhost:5000
- Admin:     http://localhost:5000/admin
- SSE:       /api/pedidos/stream (tempo real)
"""

import os
import re
import json
import time
import base64
import io
import unicodedata
import threading
import requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template

try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M
    QRCODE_OK = True
except ImportError:
    QRCODE_OK = False


# ============================================================
# ⚙️ CONFIGURAÇÕES
# ============================================================
MP_USAR = False
MP_ACCESS_TOKEN = ""
MP_PAYER_EMAIL  = "cliente@example.com"

# ✅ SUA CHAVE PIX (telefone) - TITULAR: YAN SILVA SANTANA
PIX_CHAVE  = "71993606999"
PIX_NOME   = "YAN SILVA SANTANA"
PIX_CIDADE = "SALVADOR"

# ✅ ABERTO TODOS OS DIAS DAS 11H À MEIA-NOITE
HORARIOS = {
    0: (11, 24),   # Segunda
    1: (11, 24),   # Terça
    2: (11, 24),   # Quarta
    3: (11, 24),   # Quinta
    4: (11, 24),   # Sexta
    5: (11, 24),   # Sábado
    6: (11, 24),   # Domingo
}


# ============================================================
# APP
# ============================================================
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
PEDIDOS_DIR = os.path.join(BASE_DIR, "pedidos")
os.makedirs(PEDIDOS_DIR, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")


# ============================================================
# FILA SSE
# ============================================================
_eventos_pedidos = []
_lock_eventos = threading.Lock()

def notificar_novo_pedido(pedido):
    with _lock_eventos:
        _eventos_pedidos.append(pedido)
        if len(_eventos_pedidos) > 50:
            _eventos_pedidos.pop(0)


@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# ============================================================
# PIX — Bacen
# ============================================================
def _sanitize(texto, max_len):
    if not texto: return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[^A-Za-z0-9 ]", "", texto).strip()
    return texto.upper()[:max_len]

def _tlv(tag, value):
    value = str(value)
    return f"{tag}{len(value):02d}{value}"

def _crc16(payload):
    crc = 0xFFFF
    for ch in payload:
        crc ^= ord(ch) << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"

def gerar_payload_pix_bacen(valor, txid="***"):
    if valor is None or float(valor) <= 0:
        raise ValueError("Valor deve ser maior que zero.")
    txid = re.sub(r"[^A-Za-z0-9]", "", txid or "")[:25] or "***"
    nome = _sanitize(PIX_NOME, 25)
    cidade = _sanitize(PIX_CIDADE, 15)
    mai = _tlv("00", "br.gov.bcb.pix") + _tlv("01", PIX_CHAVE)
    adf = _tlv("05", txid)
    payload  = _tlv("00", "01")
    payload += _tlv("26", mai)
    payload += _tlv("52", "0000")
    payload += _tlv("53", "986")
    payload += _tlv("54", f"{float(valor):.2f}")
    payload += _tlv("58", "BR")
    payload += _tlv("59", nome)
    payload += _tlv("60", cidade)
    payload += _tlv("62", adf)
    payload += "6304"
    return payload + _crc16(payload)

def gerar_qrcode_bacen_base64(payload):
    if not QRCODE_OK:
        raise RuntimeError("pip install qrcode[pil]")
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ============================================================
# MERCADO PAGO (opcional)
# ============================================================
def gerar_pix_mercadopago(valor, txid):
    if not MP_ACCESS_TOKEN or "COLE_O_NOVO_TOKEN" in MP_ACCESS_TOKEN or "COLE_AQUI" in MP_ACCESS_TOKEN:
        raise RuntimeError("Configure MP_ACCESS_TOKEN no server.py.")
    url = "https://api.mercadopago.com/v1/payments"
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": txid,
    }
    body = {
        "transaction_amount": round(float(valor), 2),
        "description": "Pedido Pizzaria Pizzaí",
        "payment_method_id": "pix",
        "payer": {"email": MP_PAYER_EMAIL, "first_name": "Cliente", "last_name": "Pizzai"},
    }
    try:
        r = requests.post(url, json=body, headers=headers, timeout=20)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Erro de conexão com Mercado Pago: {e}")
    if r.status_code not in (200, 201):
        try:
            erro_json = r.json()
            msg = erro_json.get("message") or erro_json.get("error") or "erro desconhecido"
            cause = erro_json.get("cause", [])
            detalhes = ""
            if cause:
                detalhes = " | " + "; ".join([str(c.get("description") or c) for c in cause])
            raise RuntimeError(f"MP {r.status_code}: {msg}{detalhes}")
        except ValueError:
            raise RuntimeError(f"MP {r.status_code}: {r.text[:300]}")
    data = r.json()
    point = data.get("point_of_interaction", {}) or {}
    tx_data = point.get("transaction_data", {}) or {}
    qr_b64  = tx_data.get("qr_code_base64", "")
    payload = tx_data.get("qr_code", "")
    ticket  = tx_data.get("ticket_url", "")
    if not qr_b64 or not payload:
        raise RuntimeError("Mercado Pago não retornou QR Code.")
    if not qr_b64.startswith("data:"):
        qr_b64 = "data:image/png;base64," + qr_b64
    return {"id_mp": data.get("id"), "qrcode_base64": qr_b64, "payload": payload, "ticket_url": ticket}


def loja_esta_aberta():
    agora = datetime.now()
    faixa = HORARIOS.get(agora.weekday())
    if not faixa: return False
    abre, fecha = faixa
    h = agora.hour
    if fecha >= 24: return h >= abre
    return abre <= h < fecha


# ============================================================
# ROTAS
# ============================================================
@app.route("/")
def index():
    try:
        return render_template("index.html")
    except Exception as e:
        return f"<h1>Erro</h1><p>Coloque <code>index.html</code> em <code>templates/</code>.</p><pre>{e}</pre>", 500


@app.route("/admin")
def admin():
    try:
        return render_template("admin.html")
    except Exception as e:
        return f"<h1>Erro</h1><p>Coloque <code>admin.html</code> em <code>templates/</code>.</p><pre>{e}</pre>", 500


@app.route("/api/status")
def api_status():
    aberto = loja_esta_aberta()
    return jsonify({
        "aberto": aberto,
        "mensagem": "Aberto agora 🍕" if aberto else "Fechado no momento 😴",
        "horarios": {"todos_os_dias": "11h às 00h"},
    })


@app.route("/api/pix", methods=["POST", "OPTIONS"])
def api_pix():
    if request.method == "OPTIONS": return ("", 204)
    try:
        data = request.get_json(force=True, silent=True) or {}
        valor_raw = data.get("valor")
        if valor_raw is None:
            return jsonify({"erro": "Campo 'valor' é obrigatório."}), 400
        try:
            valor = float(valor_raw)
        except (TypeError, ValueError):
            return jsonify({"erro": "Valor inválido."}), 400
        if valor <= 0:
            return jsonify({"erro": "Valor deve ser maior que zero."}), 400
        if valor > 10000:
            return jsonify({"erro": "Valor acima do permitido."}), 400

        txid = data.get("txid") or f"PIZZAI{int(datetime.now().timestamp())}"
        txid = re.sub(r"[^A-Za-z0-9]", "", str(txid))[:25] or "***"

        if MP_USAR:
            info = gerar_pix_mercadopago(valor, txid)
            return jsonify({
                "ok": True, "gateway": "mercadopago",
                "valor": round(valor, 2), "txid": txid,
                "payload": info["payload"], "qrcode_base64": info["qrcode_base64"],
                "ticket_url": info["ticket_url"], "id_mp": info["id_mp"],
            })

        payload = gerar_payload_pix_bacen(valor, txid)
        qr_b64 = gerar_qrcode_bacen_base64(payload)
        return jsonify({
            "ok": True, "gateway": "bacen",
            "valor": round(valor, 2), "txid": txid,
            "chave": PIX_CHAVE,
            "payload": payload, "qrcode_base64": qr_b64,
        })
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    except Exception as e:
        app.logger.exception("Erro em /api/pix")
        return jsonify({"erro": f"Erro interno: {e}"}), 500


@app.route("/api/pedido", methods=["POST", "OPTIONS"])
def api_pedido():
    if request.method == "OPTIONS": return ("", 204)
    try:
        data = request.get_json(force=True, silent=True) or {}

        numero = data.get("numero") or data.get("numero_cliente") or "sem-numero"
        data["numero"] = numero
        data["numero_cliente"] = numero

        numero_safe = re.sub(r"[^A-Za-z0-9_\-]", "", numero) or "pedido"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        data["recebido_em"] = datetime.now().isoformat()
        data["status"] = "novo"
        arquivo = f"{ts}_{numero_safe}.json"
        with open(os.path.join(PEDIDOS_DIR, arquivo), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        notificar_novo_pedido(data)
        app.logger.info(f"[PEDIDO] {numero} → {arquivo}")
        return jsonify({"ok": True, "arquivo": arquivo})
    except Exception as e:
        app.logger.exception("Erro em /api/pedido")
        return jsonify({"erro": str(e)}), 500


@app.route("/api/pedidos")
def api_pedidos():
    try:
        arquivos = sorted([f for f in os.listdir(PEDIDOS_DIR) if f.endswith(".json")], reverse=True)
        pedidos = []
        for nome in arquivos[:200]:
            try:
                with open(os.path.join(PEDIDOS_DIR, nome), "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data.setdefault("recebido_em", nome[:15])
                    data.setdefault("status", "novo")
                    if not data.get("numero"):
                        data["numero"] = data.get("numero_cliente") or nome
                    pedidos.append(data)
            except Exception:
                continue
        return jsonify({"ok": True, "total": len(pedidos), "pedidos": pedidos})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/api/pedido/<numero>/status", methods=["POST", "OPTIONS"])
def api_pedido_status(numero):
    """Muda o status de um pedido — SALVA NO SERVIDOR (arquivo JSON)."""
    if request.method == "OPTIONS": return ("", 204)
    try:
        data = request.get_json(force=True, silent=True) or {}
        novo_status = data.get("status") or "novo"
        if novo_status not in ("novo","preparo","rota","entregue","cancelado"):
            return jsonify({"erro": "Status inválido"}), 400

        numero_safe = re.sub(r"[^A-Za-z0-9_\-]", "", numero)
        achou = False
        for nome in os.listdir(PEDIDOS_DIR):
            if nome.endswith(".json") and numero_safe in nome:
                caminho = os.path.join(PEDIDOS_DIR, nome)
                with open(caminho, "r", encoding="utf-8") as f:
                    pedido = json.load(f)
                pedido["status"] = novo_status
                pedido.setdefault("numero", numero)
                with open(caminho, "w", encoding="utf-8") as f:
                    json.dump(pedido, f, ensure_ascii=False, indent=2)
                achou = True
                notificar_novo_pedido(pedido)
                break

        if not achou:
            return jsonify({"erro": "Pedido não encontrado"}), 404
        return jsonify({"ok": True, "status": novo_status})
    except Exception as e:
        app.logger.exception("Erro em /api/pedido/status")
        return jsonify({"erro": str(e)}), 500


@app.route("/api/pedidos/stream")
def api_pedidos_stream():
    def gerar():
        enviados = 0
        yield "event: ping\ndata: conectado\n\n"
        try:
            while True:
                with _lock_eventos:
                    novos = _eventos_pedidos[enviados:]
                    enviados = len(_eventos_pedidos)
                for p in novos:
                    yield "event: pedido\ndata: " + json.dumps(p, ensure_ascii=False) + "\n\n"
                yield "event: ping\ndata: ok\n\n"
                time.sleep(2)
        except GeneratorExit:
            return
    resp = app.response_class(gerar(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "gateway": "mercadopago" if MP_USAR else "bacen",
        "pix_chave": PIX_CHAVE,
        "pix_nome": PIX_NOME,
        "pix_cidade": PIX_CIDADE,
        "qrcode_lib": QRCODE_OK,
        "aberto": loja_esta_aberta(),
        "hora_servidor": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.errorhandler(404)
def not_found(e): return jsonify({"erro": "Rota não encontrada."}), 404
@app.errorhandler(500)
def server_error(e): return jsonify({"erro": "Erro interno do servidor."}), 500


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("🍕  Servidor Pizzaria Pizzaí")
    print("=" * 60)
    print(f"  Delivery (clientes) : http://localhost:5000")
    print(f"  Admin (pizzaria)    : http://localhost:5000/admin")
    print(f"  Gateway             : {'MERCADO PAGO' if MP_USAR else 'Pix direto (Bacen)'}")
    print(f"  Chave Pix           : {PIX_CHAVE}")
    print(f"  Titular Pix         : {PIX_NOME}")
    print(f"  Cidade Pix          : {PIX_CIDADE}")
    print(f"  QR Code lib         : {'OK' if QRCODE_OK else 'NAO INSTALADA'}")
    print(f"  Loja agora          : {'ABERTA' if loja_esta_aberta() else 'FECHADA'}")
    print(f"  SSE (tempo real)    : ATIVO")
    print(f"  Pedidos em          : {PEDIDOS_DIR}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)