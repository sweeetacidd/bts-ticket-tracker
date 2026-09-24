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


TIPO_KEYWORDS_VALIDOS = [
    "arquibancada", "cadeira", "camarote", "pista", "vip",
    "meia-entrada", "inteira", "front stage", "lounge", "setor",
]


def parece_tipo_de_ingresso(linha: str) -> bool:
    """Só aceita como 'tipo' linhas que realmente parecem uma categoria de
    ingresso (evita pegar avisos como 'Em correção' ou textos de WhatsApp)."""
    l = linha.lower()
    if len(linha) > 40:
        return False
    return any(kw in l for kw in TIPO_KEYWORDS_VALIDOS)


def extrair_precos(texto_pagina: str):
    """Lê o texto linha por linha: quando uma linha é só um preço,
    usa a última linha de texto "normal" anterior como o tipo do ingresso,
    mas só se essa linha realmente parecer uma categoria de ingresso."""
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
            if ultima_linha_texto and parece_tipo_de_ingresso(ultima_linha_texto):
                tipo = ultima_linha_texto.title()
            else:
                tipo = "Ingresso"
            resultados.append({"preco": preco, "tipo": tipo})
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

            if len(precos) < 2:
                print("  [AVISO] Só achei 1 preço — provavelmente o site mostrou "
                      "só o resumo (instabilidade), não a lista completa de "
                      "categorias. Ignorando esta leitura pra não comparar com "
                      "um valor não confiável.")
                continue

            mais_barato = min(precos, key=lambda x: x["preco"])
            # Compara com o último preço que JÁ FOI AVISADO (não com o último
            # visto), para calcular corretamente aumento ou queda real.
            anterior = estado.get(chave)

            deve_avisar = False
            variacao_pct = None
            e_primeira_vez = anterior is None

            if e_primeira_vez:
                deve_avisar = True
            elif mais_barato["preco"] != anterior["preco"]:
                deve_avisar = True
                variacao_pct = round(
                    (mais_barato["preco"] - anterior["preco"]) / anterior["preco"] * 100, 1
                )

            if deve_avisar:
                linhas = [
                    f"*🎟 Ingresso encontrado — {alvo['site']}*",
                    f"Dia: {alvo['dia']}",
                    f"Tipo: {mais_barato['tipo']}",
                    f"Preço mais barato: R$ {mais_barato['preco']:.2f}".replace(".", ","),
                ]
                if e_primeira_vez:
                    linhas.append("(primeiro preço encontrado para este dia)")
                elif variacao_pct is not None and variacao_pct < 0:
                    linhas.append(f"📉 Queda de {abs(variacao_pct)}% em relação ao último preço avisado")
                elif variacao_pct is not None and variacao_pct > 0:
                    linhas.append(f"📈 Aumento de {variacao_pct}% em relação ao último preço avisado")
                linhas.append(alvo["url"])
                enviar_telegram("\n".join(linhas))
                print("  -> Alerta enviado!")

                # Só atualiza o "último preço avisado" quando de fato avisamos,
                # para a próxima comparação ser sempre contra o último aviso.
                estado[chave] = {
                    "preco": mais_barato["preco"],
                    "tipo": mais_barato["tipo"],
                    "atualizado_em": agora,
                }
            else:
                print("  Preço igual ao último aviso, não precisa notificar de novo.")

    salvar_estado(estado)


if __name__ == "__main__":
    sys.exit(main())
