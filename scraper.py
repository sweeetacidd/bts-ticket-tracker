"""
Rastreador de preços de ingressos - BTS World Tour Arirang (Brasil)
Monitora BuyTicket (revenda) e Ticketmaster (oficial) e avisa no Telegram
quando aparecerem ingressos novos ou o preço mais barato cair.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
import requests

STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TARGETS = [
    {"site": "BuyTicket", "dia": "28/10", "url": "https://buyticketbrasil.com/event/bts-2026worldtourarirang/session/e4ac9384-ccd7-4f29-82b8-7ba8677bc2ed"},
    {"site": "BuyTicket", "dia": "30/10", "url": "https://buyticketbrasil.com/event/bts-2026worldtourarirang/session/ced30381-0cc4-4779-9c6b-ba05f44d086e"},
    {"site": "BuyTicket", "dia": "31/10", "url": "https://buyticketbrasil.com/event/bts-2026worldtourarirang/session/7dbb3a2b-985d-4bf2-9d36-df893169e098"},
    {"site": "Ticketmaster", "dia": "28/10", "url": "https://www.ticketmaster.com.br/event/venda-geral-bts-world-tour-arirang-28-10"},
    {"site": "Ticketmaster", "dia": "30/10", "url": "https://www.ticketmaster.com.br/event/venda-geral-bts-world-tour-arirang-30-10"},
    {"site": "Ticketmaster", "dia": "31/10", "url": "https://www.ticketmaster.com.br/event/venda-geral-bts-world-tour-arirang-31-10"},
]

PRICE_RE = re.compile(r"R\$\s?\d{1,3}(?:\.\d{3})*(?:,\d{2})?")
PRICE_FULL_RE = re.compile(r"^R\$\s?\d{1,3}(?:\.\d{3})*(?:,\d{2})?$")

IGNORAR_COMO_TIPO = {
    "comprar", "vender", "menor preço", "selecione o tipo de ingresso",
    "selecione a categoria", "tipo de ingresso", "categoria",
    "selecione o ingresso",
}


def parse_price(price_str: str) -> float:
    numero = price_str.replace("R$", "").strip()
    numero = numero.replace(".", "")
    numero = numero.replace(",", ".")
    return float(numero)


def extrair_precos(texto_pagina: str):
    """Lê o texto linha por linha: quando uma linha é só um preço,
    usa a última linha de texto "normal" anterior como o tipo do ingresso
    (ex: 'Arquibancada' logo acima de 'R$ 2.500')."""
    linhas = [l.strip() for l in texto_pagina.split("\n") if l.strip()]
    resultados = []
    ultima_linha_texto = None

    for linha in linhas:
        e_preco_puro = bool(PRICE_FULL_RE.match(linha))
        if e_preco_puro:
            try:
                preco = parse_price(linha)
            except ValueError:
                continue
            if preco <= 0:
                continue
            tipo = ultima_linha_texto or "Ingresso"
            resultados.append({"preco": preco, "tipo": tipo.title()})
        else:
            if linha.lower() not in IGNORAR_COMO_TIPO and not PRICE_RE.search(linha):
                ultima_linha_texto = linha

    return resultados


def carregar_pagina(playwright, url: str) -> str:
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    contexto = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="pt-BR",
        viewport={"width": 1366, "height": 768},
        extra_http_headers={
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    contexto.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    pagina = contexto.new_page()
    try:
        pagina.goto(url, timeout=45000, wait_until="networkidle")
        pagina.wait_for_timeout(4000)

        try:
            campo = pagina.get_by_text("Selecione o tipo de ingresso", exact=False)
            campo.first.click(timeout=8000)
            pagina.wait_for_timeout(2000)
        except Exception:
            pass

        texto = pagina.inner_text("body")
    finally:
        browser.close()
    return texto


def enviar_telegram(mensagem: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[AVISO] Telegram não configurado, pulando envio.")
        print(mensagem)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "Markdown",
    })
    if resp.status_code != 200:
        print(f"[ERRO] Falha ao enviar Telegram: {resp.status_code} {resp.text}")


def carregar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_estado(estado):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def main():
    estado = carregar_estado()
    agora = datetime.now(timezone.utc).isoformat()

    with sync_playwright() as p:
        for alvo in TARGETS:
            chave = alvo["url"]
            print(f"Verificando {alvo['site']} - dia {alvo['dia']}...")
            try:
                texto = carregar_pagina(p, alvo["url"])
            except Exception as e:
                print(f"[ERRO] Não consegui carregar {alvo['url']}: {e}")
                continue

            print(f"  (Texto capturado: {len(texto)} caracteres)")
            print(f"  (Amostra do início: {texto[:300]!r})")

            precos = extrair_precos(texto)
            if not precos:
                print("  Nenhum preço encontrado.")
                continue

            print(f"  ({len(precos)} preços encontrados)")

            mais_barato = min(precos, key=lambda x: x["preco"])
            anterior = estado.get(chave)

            deve_avisar = False
            desconto_pct = None

            if anterior is None:
                deve_avisar = True
            elif mais_barato["preco"] < anterior["preco"]:
                deve_avisar = True
                desconto_pct = round((1 - mais_barato["preco"] / anterior["preco"]) * 100, 1)

            if deve_avisar:
                linhas = [
                    f"*🎟 Ingresso encontrado — {alvo['site']}*",
                    f"Dia: {alvo['dia']}",
                    f"Tipo: {mais_barato['tipo']}",
                    f"Preço mais barato: R$ {mais_barato['preco']:.2f}".replace(".", ","),
                ]
                if desconto_pct is not None:
                    linhas.append(f"Queda em relação ao último preço: {desconto_pct}%")
                linhas.append(alvo["url"])
                enviar_telegram("\n".join(linhas))
                print("  -> Alerta enviado!")

            estado[chave] = {
                "preco": mais_barato["preco"],
                "tipo": mais_barato["tipo"],
                "atualizado_em": agora,
            }

    salvar_estado(estado)


if __name__ == "__main__":
    sys.exit(main())# Aceita "R$ 2.500" (sem centavos) ou "R$ 2.500,00" (com centavos)
PRICE_RE = re.compile(r"R\$\s?\d{1,3}(?:\.\d{3})*(?:,\d{2})?")


def parse_price(price_str: str) -> float:
    numero = price_str.replace("R$", "").strip()
    numero = numero.replace(".", "")
    numero = numero.replace(",", ".")
    return float(numero)


def guess_tipo(texto_ao_redor: str) -> str:
    texto_lower = texto_ao_redor.lower()
    for kw in TIPO_KEYWORDS:
        if kw in texto_lower:
            return kw.title()
    return "Ingresso"


def extrair_precos(texto_pagina: str):
    resultados = []
    for match in PRICE_RE.finditer(texto_pagina):
        preco_str = match.group()
        try:
            preco = parse_price(preco_str)
        except ValueError:
            continue
        if preco <= 0:
            continue  # ignora "R$ 0" (esgotado / indisponível)
        inicio = max(0, match.start() - 60)
        contexto = texto_pagina[inicio:match.start()]
        tipo = guess_tipo(contexto)
        resultados.append({"preco": preco, "tipo": tipo})
    return resultados


def carregar_pagina(playwright, url: str) -> str:
    browser = playwright.chromium.launch(headless=True)
    contexto = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="pt-BR",
    )
    pagina = contexto.new_page()
    try:
        pagina.goto(url, timeout=45000, wait_until="networkidle")
        pagina.wait_for_timeout(4000)

        # Tenta clicar na caixinha "Selecione o tipo de ingresso" para abrir
        # a lista com todos os tipos e preços (BuyTicket).
        try:
            campo = pagina.get_by_text("Selecione o tipo de ingresso", exact=False)
            campo.first.click(timeout=8000)
            pagina.wait_for_timeout(2000)
        except Exception:
            pass  # se não achar esse campo (ex: outro site), segue sem clicar

        texto = pagina.inner_text("body")
    finally:
        browser.close()
    return texto


def enviar_telegram(mensagem: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[AVISO] Telegram não configurado, pulando envio.")
        print(mensagem)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "Markdown",
    })
    if resp.status_code != 200:
        print(f"[ERRO] Falha ao enviar Telegram: {resp.status_code} {resp.text}")


def carregar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_estado(estado):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def main():
    estado = carregar_estado()
    agora = datetime.now(timezone.utc).isoformat()

    with sync_playwright() as p:
        for alvo in TARGETS:
            chave = alvo["url"]
            print(f"Verificando {alvo['site']} - dia {alvo['dia']}...")
            try:
                texto = carregar_pagina(p, alvo["url"])
            except Exception as e:
                print(f"[ERRO] Não consegui carregar {alvo['url']}: {e}")
                continue

            print(f"  (Texto capturado: {len(texto)} caracteres)")
            print(f"  (Amostra do início: {texto[:300]!r})")

            precos = extrair_precos(texto)
            if not precos:
                print("  Nenhum preço encontrado.")
                continue

            print(f"  ({len(precos)} preços encontrados)")

            mais_barato = min(precos, key=lambda x: x["preco"])
            anterior = estado.get(chave)

            deve_avisar = False
            desconto_pct = None

            if anterior is None:
                deve_avisar = True
            elif mais_barato["preco"] < anterior["preco"]:
                deve_avisar = True
                desconto_pct = round((1 - mais_barato["preco"] / anterior["preco"]) * 100, 1)

            if deve_avisar:
                linhas = [
                    f"*🎟 Ingresso encontrado — {alvo['site']}*",
                    f"Dia: {alvo['dia']}",
                    f"Tipo: {mais_barato['tipo']}",
                    f"Preço mais barato: R$ {mais_barato['preco']:.2f}".replace(".", ","),
                ]
                if desconto_pct is not None:
                    linhas.append(f"Queda em relação ao último preço: {desconto_pct}%")
                linhas.append(alvo["url"])
                enviar_telegram("\n".join(linhas))
                print("  -> Alerta enviado!")

            estado[chave] = {
                "preco": mais_barato["preco"],
                "tipo": mais_barato["tipo"],
                "atualizado_em": agora,
            }

    salvar_estado(estado)


if __name__ == "__main__":
    sys.exit(main())
def parse_price(price_str: str) -> float:
    numero = price_str.replace("R$", "").strip()
    numero = numero.replace(".", "").replace(",", ".")
    return float(numero)


def guess_tipo(texto_ao_redor: str) -> str:
    texto_lower = texto_ao_redor.lower()
    for kw in TIPO_KEYWORDS:
        if kw in texto_lower:
            return kw.title()
    return "Ingresso"


def extrair_precos(texto_pagina: str):
    resultados = []
    for match in PRICE_RE.finditer(texto_pagina):
        preco_str = match.group()
        try:
            preco = parse_price(preco_str)
        except ValueError:
            continue
        inicio = max(0, match.start() - 60)
        contexto = texto_pagina[inicio:match.start()]
        tipo = guess_tipo(contexto)
        resultados.append({"preco": preco, "tipo": tipo})
    return resultados


def carregar_pagina(playwright, url: str) -> str:
    browser = playwright.chromium.launch(headless=True)
    contexto = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="pt-BR",
    )
    pagina = contexto.new_page()
    try:
        pagina.goto(url, timeout=45000, wait_until="networkidle")
        pagina.wait_for_timeout(3000)
        texto = pagina.inner_text("body")
    finally:
        browser.close()
    return texto


def enviar_telegram(mensagem: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[AVISO] Telegram não configurado, pulando envio.")
        print(mensagem)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "Markdown",
    })
    if resp.status_code != 200:
        print(f"[ERRO] Falha ao enviar Telegram: {resp.status_code} {resp.text}")


def carregar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_estado(estado):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def main():
    estado = carregar_estado()
    agora = datetime.now(timezone.utc).isoformat()

    with sync_playwright() as p:
        for alvo in TARGETS:
            chave = alvo["url"]
            print(f"Verificando {alvo['site']} - dia {alvo['dia']}...")
            try:
                texto = carregar_pagina(p, alvo["url"])
            except Exception as e:
                print(f"[ERRO] Não consegui carregar {alvo['url']}: {e}")
                continue

            # --- DEBUG: pistas pra entender o que a página realmente mostrou ---
            print(f"  (Texto capturado: {len(texto)} caracteres)")
            print(f"  (Amostra do início: {texto[:300]!r})")
            # ---------------------------------------------------------------

            precos = extrair_precos(texto)
            if not precos:
                print("  Nenhum preço encontrado.")
                continue

            mais_barato = min(precos, key=lambda x: x["preco"])
            anterior = estado.get(chave)

            deve_avisar = False
            desconto_pct = None

            if anterior is None:
                deve_avisar = True
            elif mais_barato["preco"] < anterior["preco"]:
                deve_avisar = True
                desconto_pct = round((1 - mais_barato["preco"] / anterior["preco"]) * 100, 1)

            if deve_avisar:
                linhas = [
                    f"*🎟 Ingresso encontrado — {alvo['site']}*",
                    f"Dia: {alvo['dia']}",
                    f"Tipo: {mais_barato['tipo']}",
                    f"Preço mais barato: R$ {mais_barato['preco']:.2f}".replace(".", ","),
                ]
                if desconto_pct is not None:
                    linhas.append(f"Queda em relação ao último preço: {desconto_pct}%")
                linhas.append(alvo["url"])
                enviar_telegram("\n".join(linhas))
                print("  -> Alerta enviado!")

            estado[chave] = {
                "preco": mais_barato["preco"],
                "tipo": mais_barato["tipo"],
                "atualizado_em": agora,
            }

    salvar_estado(estado)


if __name__ == "__main__":
    sys.exit(main())
def parse_price(price_str: str) -> float:
    numero = price_str.replace("R$", "").strip()
    numero = numero.replace(".", "").replace(",", ".")
    return float(numero)


def guess_tipo(texto_ao_redor: str) -> str:
    texto_lower = texto_ao_redor.lower()
    for kw in TIPO_KEYWORDS:
        if kw in texto_lower:
            return kw.title()
    return "Ingresso"


def extrair_precos(texto_pagina: str):
    resultados = []
    for match in PRICE_RE.finditer(texto_pagina):
        preco_str = match.group()
        try:
            preco = parse_price(preco_str)
        except ValueError:
            continue
        inicio = max(0, match.start() - 60)
        contexto = texto_pagina[inicio:match.start()]
        tipo = guess_tipo(contexto)
        resultados.append({"preco": preco, "tipo": tipo})
    return resultados


def carregar_pagina(playwright, url: str) -> str:
    browser = playwright.chromium.launch(headless=True)
    contexto = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="pt-BR",
    )
    pagina = contexto.new_page()
    try:
        pagina.goto(url, timeout=45000, wait_until="networkidle")
        pagina.wait_for_timeout(3000)
        texto = pagina.inner_text("body")
    finally:
        browser.close()
    return texto


def enviar_telegram(mensagem: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[AVISO] Telegram não configurado, pulando envio.")
        print(mensagem)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "Markdown",
    })
    if resp.status_code != 200:
        print(f"[ERRO] Falha ao enviar Telegram: {resp.status_code} {resp.text}")


def carregar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_estado(estado):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def main():
    estado = carregar_estado()
    agora = datetime.now(timezone.utc).isoformat()

    with sync_playwright() as p:
        for alvo in TARGETS:
            chave = alvo["url"]
            print(f"Verificando {alvo['site']} - dia {alvo['dia']}...")
            try:
                texto = carregar_pagina(p, alvo["url"])
            except Exception as e:
                print(f"[ERRO] Não consegui carregar {alvo['url']}: {e}")
                continue

            precos = extrair_precos(texto)
            if not precos:
                print("  Nenhum preço encontrado.")
                continue

            mais_barato = min(precos, key=lambda x: x["preco"])
            anterior = estado.get(chave)

            deve_avisar = False
            desconto_pct = None

            if anterior is None:
                deve_avisar = True
            elif mais_barato["preco"] < anterior["preco"]:
                deve_avisar = True
                desconto_pct = round((1 - mais_barato["preco"] / anterior["preco"]) * 100, 1)

            if deve_avisar:
                linhas = [
                    f"*🎟 Ingresso encontrado — {alvo['site']}*",
                    f"Dia: {alvo['dia']}",
                    f"Tipo: {mais_barato['tipo']}",
                    f"Preço mais barato: R$ {mais_barato['preco']:.2f}".replace(".", ","),
                ]
                if desconto_pct is not None:
                    linhas.append(f"Queda em relação ao último preço: {desconto_pct}%")
                linhas.append(alvo["url"])
                enviar_telegram("\n".join(linhas))
                print("  -> Alerta enviado!")

            estado[chave] = {
                "preco": mais_barato["preco"],
                "tipo": mais_barato["tipo"],
                "atualizado_em": agora,
            }

    salvar_estado(estado)


if __name__ == "__main__":
    sys.exit(main())
