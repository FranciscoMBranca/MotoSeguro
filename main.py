import uasyncio as asyncio
from machine import UART, Pin
import time, math, network, ujson, gc

# ---------------------------------------------------------------------------
# Configuração de pinos e UARTs
# ---------------------------------------------------------------------------
GPS_UART_ID, GPS_BAUDRATE = 1, 9600
GPS_RX_PIN,  GPS_TX_PIN   = 16, 17
GSM_UART_ID, GSM_BAUDRATE = 2, 9600
GSM_RX_PIN,  GSM_TX_PIN   = 26, 27
SENSOR_VEL_PIN = 34
IGNICAO_PIN    = 22
ATUADOR_PIN    = 32
LED_PIN        = 33
BUZZER_PIN     = 25
BOTAO_PIN      = 14

# ---------------------------------------------------------------------------
# Parâmetros do sistema
# ---------------------------------------------------------------------------
NUMERO_PROPRIETARIO = "+244xxxxxxxxx"
RAIO_RODA_M         = 0.28
CIRCUNFERENCIA_M    = 2 * math.pi * RAIO_RODA_M
LIMITE_VEL_PADRAO   = 80
MARGEM_KMH          = 5
SMS_INTERVALO_S     = 150
AP_SSID, AP_SENHA   = "MotoSeguro", "motoseguro"


# ===========================================================================
# ModuloGPS
# ===========================================================================
class ModuloGPS:
    def __init__(self):
        self.uart = UART(GPS_UART_ID, baudrate=GPS_BAUDRATE,
                         tx=GPS_TX_PIN, rx=GPS_RX_PIN)
        self.latitude, self.longitude = None, None
        self.vel_kmh, self.fix_valido = 0.0, False

    @staticmethod
    def _nmea_to_dec(val, hemi):
        """Converte coordenada NMEA (DDDMM.MMMMM) para graus decimais."""
        if not val:
            return None
        v = float(val)
        g = int(v / 100)
        m = v - g * 100
        d = g + (m / 60.0)
        return -d if hemi in ('S', 'W') else d

    async def atualizar(self):
        if not self.uart.any():
            return
        try:
            dados = self.uart.read().decode('ascii', 'ignore')
            for linha in dados.split('\n'):
                if not linha.startswith('$GPRMC'):
                    continue
                campos = linha.strip().split(',')
                if len(campos) < 10:
                    continue
                self.fix_valido = (campos[2] == 'A')
                if not self.fix_valido:
                    continue
                try:
                    self.latitude  = self._nmea_to_dec(campos[3], campos[4])
                    self.longitude = self._nmea_to_dec(campos[5], campos[6])
                    self.vel_kmh   = float(campos[7]) * 1.852 if campos[7] else 0.0
                except (ValueError, IndexError):
                    pass
        except Exception:
            pass

    def obter_texto_sms(self):
        if self.fix_valido and self.latitude is not None:
            link = "https://maps.google.com/?q={:.6f},{:.6f}".format(
                self.latitude, self.longitude)
            return "GPS OK\nLat: {:.6f}\nLon: {:.6f}\nMapa: {}".format(
                self.latitude, self.longitude, link)
        return "GPS: Sem sinal valido."


# ===========================================================================
# ModuloGSM  (SIM900)
# ===========================================================================
class ModuloGSM:
    def __init__(self):
        self.uart = UART(GSM_UART_ID, baudrate=GSM_BAUDRATE,
                         tx=GSM_TX_PIN, rx=GSM_RX_PIN)
        self.inicializado = False

    def _limpar_uart(self):
        """Limpa buffer UART completamente."""
        while self.uart.any():
            try:
                self.uart.read()
            except:
                break

    async def cmd(self, comando, espera_ms=2000, ok="OK"):
        # CORRECÇÃO: time.time() é inteiro (segundos) — ticks_ms dá precisão ms
        self._limpar_uart()
        self.uart.write((comando + "\r\n").encode())
        resposta = ""
        t_fim = time.ticks_add(time.ticks_ms(), espera_ms)
        while time.ticks_diff(t_fim, time.ticks_ms()) > 0:
            if self.uart.any():
                trecho = self.uart.read().decode('ascii', 'ignore')
                resposta += trecho
                if ok in resposta or "ERROR" in resposta:
                    break
            await asyncio.sleep_ms(50)
        return resposta

    async def inicializar(self):
        print("[GSM] Iniciando SIM900...")
        r = await self.cmd("AT", espera_ms=1000)
        if "OK" not in r:
            return False
        await self.cmd("ATE0")
        await self.cmd("AT+CMGF=1")
        await self.cmd('AT+CSCS="GSM"')
        await self.cmd("AT+CMGD=1,4", espera_ms=5000)   # limpa SMS antigos
        reg = await self.cmd("AT+CREG?", espera_ms=3000)
        print("[GSM] Rede: {}".format(reg.strip()))
        self.inicializado = True
        return True

    async def enviar_sms(self, numero, mensagem):
        if not self.inicializado:
            return False
        try:
            self._limpar_uart()
            self.uart.write('AT+CMGS="{}"\r\n'.format(numero).encode())
            # CORRECÇÃO: usar ticks_ms em vez de time.time()
            t_fim = time.ticks_add(time.ticks_ms(), 5000)
            pronto = False
            while time.ticks_diff(t_fim, time.ticks_ms()) > 0:
                if self.uart.any():
                    res = self.uart.read().decode('ascii', 'ignore')
                    if ">" in res:
                        pronto = True
                        break
                await asyncio.sleep_ms(50)
            if not pronto:
                return False
            self.uart.write("{}\x1A".format(mensagem).encode())
            r = ""
            t_fim = time.ticks_add(time.ticks_ms(), 10000)
            while time.ticks_diff(t_fim, time.ticks_ms()) > 0:
                if self.uart.any():
                    r += self.uart.read().decode('ascii', 'ignore')
                    if "OK" in r:
                        return True
                    if "ERROR" in r:
                        return False
                await asyncio.sleep_ms(100)
            return False
        except Exception:
            return False

    async def ler_sms(self):
        """Lê todos os SMS não lidos e devolve lista de textos (corpo)."""
        if not self.inicializado:
            return []
        r = await self.cmd('AT+CMGL="REC UNREAD"', espera_ms=5000, ok="OK")
        mensagens = []
        linhas = r.split('\n')
        for i, linha in enumerate(linhas):
            if linha.startswith("+CMGL:"):
                if i + 1 < len(linhas):
                    corpo = linhas[i + 1].strip()
                    if corpo:
                        mensagens.append(corpo)
        if mensagens:
            await self.cmd("AT+CMGD=1,4", espera_ms=5000)
        return mensagens


# ===========================================================================
# SensorVelocidade (Hall)
# ===========================================================================
class SensorVelocidade:
    def __init__(self):
        self.pino = Pin(SENSOR_VEL_PIN, Pin.IN)
        self._pulsos = 0
        self._t_ultima_med   = time.ticks_ms()
        self._t_ultimo_pulso = 0
        self.vel_kmh = 0.0
        self.pino.irq(trigger=Pin.IRQ_FALLING, handler=self._isr)
        print("[VEL] Sensor Hall pronto.")

    def _isr(self, pin):
        agora = time.ticks_ms()
        if time.ticks_diff(agora, self._t_ultimo_pulso) > 15:
            self._pulsos += 1
            self._t_ultimo_pulso = agora

    def calcular(self):
        agora = time.ticks_ms()
        dt_ms = time.ticks_diff(agora, self._t_ultima_med)
        if dt_ms < 300:
            return self.vel_kmh
        p = self._pulsos
        self._pulsos       = 0
        self._t_ultima_med = agora
        vel_nova = 0.0
        if p > 0:
            vel_ms   = (p * CIRCUNFERENCIA_M) / (dt_ms / 1000.0)
            vel_nova = vel_ms * 3.6
        self.vel_kmh = (0.7 * vel_nova) + (0.3 * self.vel_kmh)
        if self.vel_kmh < 1.0:
            self.vel_kmh = 0.0
        return self.vel_kmh


# ===========================================================================
# SensorIgnicao
# ===========================================================================
class SensorIgnicao:
    def __init__(self, pino):
        self.pino = Pin(pino, Pin.IN)

    @property
    def ligada(self):
        return self.pino.value() == 1


# ===========================================================================
# WebDashboard
# ===========================================================================
class WebDashboard:
    def __init__(self, sistema):
        self.sistema = sistema
        self.wlan    = network.WLAN(network.AP_IF)
        self.ip      = None
        self._lock   = asyncio.Lock()

    async def iniciar(self):
        self.wlan.active(True)
        self.wlan.config(essid=AP_SSID, password=AP_SENHA, authmode=3)
        self.ip = self.wlan.ifconfig()[0]
        print("[WEB] Dashboard em http://{}/".format(self.ip))
        asyncio.create_task(asyncio.start_server(self.handle_http, "0.0.0.0", 80))

    # ------------------------------------------------------------------
    async def handle_http(self, reader, writer):
        try:
            req = await reader.readline()
            if not req:
                writer.close()
                return
            
            # Drena os headers
            while True:
                line = await reader.readline()
                if not line or line == b"\r\n":
                    break

            # Extrai path e query string
            partes = req.decode().split(" ")
            full_path = partes[1] if len(partes) > 1 else "/"
            if "?" in full_path:
                path, qs = full_path.split("?", 1)
            else:
                path, qs = full_path, ""

            # Parse dos parâmetros da query string
            params = {}
            for p in qs.split("&"):
                if "=" in p:
                    k, v = p.split("=", 1)
                    params[k] = v

            # ----- /api/data -----
            if path == "/api/data":
                lat_r = round(self.sistema.gps.latitude,  6) \
                    if self.sistema.gps.latitude  is not None else 0.0
                lon_r = round(self.sistema.gps.longitude, 6) \
                    if self.sistema.gps.longitude is not None else 0.0
                dados = {
                    "velocidade": round(self.sistema.vel_kmh, 1),
                    "limite":     self.sistema.limite_ativo,
                    "estado":     self.sistema.estado,
                    "ignicao":    self.sistema.ignicao.ligada,
                    "gps_fix":    self.sistema.gps.fix_valido,
                    "lat":        lat_r,
                    "lon":        lon_r,
                    "atuador":    bool(self.sistema.pin_atuador.value()),
                }
                corpo = ujson.dumps(dados).encode()
                ctype = "application/json"

            # ----- /comando -----
            elif path == "/comando":
                cmd   = params.get("cmd", "")
                estado = int(params.get("estado", "0"))
                if cmd == "atuador":
                    # Só permite ligar manualmente fora do modo ROUBO
                    if self.sistema.estado != self.sistema.ESTADO_ROUBO:
                        self.sistema.pin_atuador.value(estado)
                elif cmd == "reset":
                    if self.sistema.estado == self.sistema.ESTADO_ROUBO:
                        self.sistema.estado = self.sistema.ESTADO_NORMAL
                        self.sistema.pin_atuador.value(0)
                        print("[WEB] Reset de roubo via dashboard.")
                corpo = b'{"ok":true}'
                ctype = "application/json"

            # ----- / (HTML) -----
            else:
                corpo = self._get_html().encode()
                ctype = "text/html; charset=utf-8"

            # Prepara headers com Content-Length
            head = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: {}\r\n"
                "Content-Length: {}\r\n"
                "Connection: close\r\n"
                "Cache-Control: no-cache, no-store, must-revalidate\r\n"
                "\r\n"
            ).format(ctype, len(corpo))
            
            # Escreve headers e corpo
            writer.write(head.encode())
            await writer.drain()
            writer.write(corpo)
            await writer.drain()
            
        except OSError as e:
            pass
        except Exception as e:
            print("[WEB] Erro: {}".format(e))
        finally:
            try:
                await writer.wait_closed()
            except:
                pass
            # Força garbage collection após cada conexão
            gc.collect()

    # ------------------------------------------------------------------
    def _get_html(self):
        # HTML compacto com design baseado no painel clínico (tema claro, Inter)
        # JS usa aspas duplas nas strings para compatibilidade com Python single-quote
        css = (
            ':root{--bg:#f4f7fa;--sf:#fff;--txt:#1e293b;--sub:#64748b;'
            '--pri:#0284c7;--ok:#0d9488;--err:#e11d48;--warn:#d97706;--bd:#e2e8f0}'
            '*{margin:0;padding:0;box-sizing:border-box;font-family:Inter,Arial,sans-serif}'
            'body{background:var(--bg);color:var(--txt);padding:16px}'
            '.wrap{max-width:1100px;margin:0 auto}'
            'header{display:flex;justify-content:space-between;align-items:center;'
            'padding:14px 20px;background:var(--sf);border-radius:14px;'
            'border:1px solid var(--bd);margin-bottom:20px;'
            'box-shadow:0 1px 3px rgba(0,0,0,.05)}'
            'h1{font-size:18px;font-weight:700}'
            '.badge{display:flex;align-items:center;gap:6px;padding:5px 12px;'
            'border-radius:9999px;font-size:12px;font-weight:600;background:#f1f5f9}'
            '.sec{font-size:14px;font-weight:700;color:var(--sub);margin:16px 0 12px}'
            '.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));'
            'gap:16px;margin-bottom:16px}'
            '.card{background:var(--sf);border-radius:14px;padding:20px;'
            'border:1px solid var(--bd);transition:all .3s}'
            '.ch{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}'
            '.ct{font-size:11px;font-weight:600;color:var(--sub);'
            'text-transform:uppercase;letter-spacing:.5px}'
            '.cv{font-size:30px;font-weight:700;display:flex;align-items:baseline;gap:4px}'
            '.cu{font-size:14px;font-weight:500;color:var(--sub)}'
            '.pb{height:6px;background:#e2e8f0;border-radius:9999px;margin-top:8px;overflow:hidden}'
            '.pf{height:100%;width:0%;transition:width .5s}'
            '.critico{border-color:var(--err)!important;background:#fff5f5;'
            'animation:pulse 2s infinite}'
            '.critico .ct{color:var(--err)}'
            '.ok-card{border-color:var(--ok)!important;background:#f0fdf4}'
            '.warn-card{border-color:var(--warn)!important}'
            '.cg{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}'
            '.ctrl{display:flex;align-items:center;justify-content:space-between;padding:18px 20px}'
            '.ci{display:flex;align-items:center;gap:14px}'
            '.cl{font-size:14px;font-weight:600}'
            '.cd{font-size:12px;color:var(--sub);margin-top:2px}'
            '.btn{padding:9px 18px;border-radius:9px;border:none;font-weight:600;'
            'font-size:12px;cursor:pointer;min-width:100px}'
            '.b-off{background:#f1f5f9;color:var(--txt);border:1px solid var(--bd)}'
            '.b-on{background:var(--err);color:#fff}'
            '.b-pri{background:var(--pri);color:#fff}'
            '.b-dis{background:#f8fafc;color:#cbd5e1;cursor:not-allowed;border:1px solid #e2e8f0}'
            '.ico{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2;'
            'stroke-linecap:round;stroke-linejoin:round}'
            '@keyframes pulse{'
            '0%{box-shadow:0 0 0 0 rgba(225,29,72,.4)}'
            '70%{box-shadow:0 0 0 10px rgba(225,29,72,0)}'
            'to{box-shadow:0 0 0 0 rgba(225,29,72,0)}}'
        )
        body = (
            '<div class="wrap">'
            '<header>'
            '<h1>MotoSeguro &#8212; Painel de Controlo</h1>'
            '<div id="badge" class="badge" style="color:var(--ok)">AP OPERACIONAL</div>'
            '</header>'

            '<div class="sec">Telemetria do Ve&#237;culo</div>'
            '<div class="grid">'
            # card estado
            '<div class="card" id="card-estado">'
            '<div class="ch"><div class="ct">Estado do Sistema</div>'
            '<svg class="ico" viewBox="0 0 24 24" style="color:var(--pri)">'
            '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></div>'
            '<div class="cv" id="val-estado" style="font-size:22px">--</div>'
            '</div>'
            # card velocidade
            '<div class="card" id="card-vel">'
            '<div class="ch"><div class="ct">Velocidade</div>'
            '<svg class="ico" viewBox="0 0 24 24" style="color:var(--warn)">'
            '<circle cx="12" cy="12" r="10"/><path d="m12 12 4-4"/></svg></div>'
            '<div class="cv"><span id="val-vel">0</span><span class="cu">km/h</span></div>'
            '<div class="pb"><div id="pb-vel" class="pf" style="background:var(--warn)"></div></div>'
            '</div>'
            # card limite
            '<div class="card">'
            '<div class="ch"><div class="ct">Limite Activo</div>'
            '<svg class="ico" viewBox="0 0 24 24" style="color:var(--err)">'
            '<circle cx="12" cy="12" r="10"/><path d="M4.93 4.93 19.07 19.07"/></svg></div>'
            '<div class="cv"><span id="val-lim">80</span><span class="cu">km/h</span></div>'
            '</div>'
            # card ignicao
            '<div class="card" id="card-ign">'
            '<div class="ch"><div class="ct">Igni&#231;&#227;o</div>'
            '<svg class="ico" viewBox="0 0 24 24" style="color:var(--ok)">'
            '<path d="m13 2-2 6.9H5L11 13l-2 6.9L15 14l6-.1-4.8-3.9L18 4l-5 3z"/></svg></div>'
            '<div class="cv" id="val-ign" style="font-size:22px">--</div>'
            '</div>'
            '</div>'  # end .grid telemetria

            '<div class="sec">Localiza&#231;&#227;o GPS (Neo-6M)</div>'
            '<div class="grid">'
            # card fix
            '<div class="card" id="card-fix">'
            '<div class="ch"><div class="ct">Estado do Fix</div>'
            '<svg class="ico" viewBox="0 0 24 24" style="color:var(--pri)">'
            '<circle cx="12" cy="12" r="3"/>'
            '<path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg></div>'
            '<div class="cv" id="val-fix" style="font-size:22px">--</div>'
            '</div>'
            # card lat
            '<div class="card">'
            '<div class="ch"><div class="ct">Latitude</div></div>'
            '<div class="cv" style="font-size:20px"><span id="val-lat">--</span></div>'
            '</div>'
            # card lon
            '<div class="card">'
            '<div class="ch"><div class="ct">Longitude</div></div>'
            '<div class="cv" style="font-size:20px"><span id="val-lon">--</span></div>'
            '</div>'
            # card mapa
            '<div class="card">'
            '<div class="ch"><div class="ct">Localiza&#231;&#227;o no Mapa</div></div>'
            '<div style="margin-top:8px;font-size:14px" id="val-mapa">Aguardando fix...</div>'
            '</div>'
            '</div>'  # end .grid gps

            '<div class="sec">Controlo de Actuadores</div>'
            '<div class="cg">'
            # ctrl atuador
            '<div class="card ctrl">'
            '<div class="ci">'
            '<svg class="ico" viewBox="0 0 24 24" style="color:var(--pri)">'
            '<circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 3"/></svg>'
            '<div><div class="cl">Actuador de Velocidade</div>'
            '<div class="cd">Limita&#231;&#227;o f&#237;sica &mdash; Pino 32</div></div>'
            '</div>'
            '<button id="btn-atuador" class="btn b-pri" onclick="enviar(\'atuador\')">LIGAR</button>'
            '</div>'
            # ctrl alarme
            '<div class="card ctrl" id="card-alarme">'
            '<div class="ci">'
            '<svg class="ico" viewBox="0 0 24 24" style="color:var(--err)">'
            '<path d="M10.3 21a1.94 1.94 0 0 0 3.4 0M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>'
            '</svg>'
            '<div><div class="cl">Alarme de Roubo</div>'
            '<div class="cd">Reset remoto do sistema</div></div>'
            '</div>'
            '<button id="btn-alarme" class="btn b-dis" onclick="enviar(\'reset\')" disabled>SEGURO</button>'
            '</div>'
            '</div>'  # end .cg
            '</div>'  # end .wrap
        )
        js = (
            'let fetching=false;'
            'async function fd(){'
            'if(fetching)return;'
            'fetching=true;'
            'try{'
            'const d=await(await fetch("/api/data?t="+Date.now())).json();'
            'document.getElementById("val-vel").innerText=d.velocidade.toFixed(1);'
            'document.getElementById("val-lim").innerText=d.limite;'
            'document.getElementById("val-estado").innerText=d.estado;'
            'document.getElementById("val-ign").innerText=d.ignicao?"LIGADA":"DESLIGADA";'
            'document.getElementById("pb-vel").style.width=Math.min((d.velocidade/200)*100,100)+"%";'
            'const ce=document.getElementById("card-estado");'
            'if(d.estado==="ROUBO"){ce.className="card critico";}'
            'else if(d.estado==="LIMITANDO"){ce.className="card warn-card";}'
            'else{ce.className="card";}'
            'document.getElementById("card-vel").className=d.velocidade>d.limite?"card critico":"card";'
            'document.getElementById("card-ign").className=d.ignicao?"card ok-card":"card critico";'
            'document.getElementById("val-fix").innerText=d.gps_fix?"FIX OK":"SEM FIX";'
            'document.getElementById("card-fix").className=d.gps_fix?"card ok-card":"card";'
            'if(d.gps_fix&&d.lat!==0){'
            'document.getElementById("val-lat").innerText=d.lat.toFixed(6)+"\u00b0";'
            'document.getElementById("val-lon").innerText=d.lon.toFixed(6)+"\u00b0";'
            'document.getElementById("val-mapa").innerHTML='
            '"<a href=\'https://maps.google.com/?q="+d.lat.toFixed(6)+","+d.lon.toFixed(6)+"\' '
            'target=\'_blank\' style=\'color:var(--pri)\'>Abrir Google Maps \u2197</a>";'
            '}else{'
            'document.getElementById("val-lat").innerText="--";'
            'document.getElementById("val-lon").innerText="--";'
            'document.getElementById("val-mapa").innerText="Aguardando fix...";'
            '}'
            'const ba=document.getElementById("btn-atuador");'
            'if(d.atuador){ba.className="btn b-on";ba.innerText="DESLIGAR";}'
            'else{ba.className="btn b-pri";ba.innerText="LIGAR";}'
            'const br=document.getElementById("btn-alarme");'
            'if(d.estado==="ROUBO"){'
            'document.getElementById("card-alarme").classList.add("critico");'
            'br.className="btn b-on";br.innerText="RESET";br.disabled=false;'
            '}else{'
            'document.getElementById("card-alarme").classList.remove("critico");'
            'br.className="btn b-dis";br.innerText="SEGURO";br.disabled=true;'
            '}'
            'document.getElementById("badge").style.color="var(--ok)";'
            'document.getElementById("badge").innerText="AP OPERACIONAL";'
            '}catch(e){'
            'console.error(e);'
            'document.getElementById("badge").style.color="var(--err)";'
            'document.getElementById("badge").innerText="OFFLINE";'
            '}'
            'fetching=false;'
            '}'
            'async function enviar(cmd){'
            'const ba=document.getElementById("btn-atuador");'
            'let est=0;'
            'if(cmd==="atuador"){est=(ba.innerText==="LIGAR")?1:0;}'
            'try{await fetch("/comando?cmd="+cmd+"&estado="+est+"&t="+Date.now(),{method:"POST"});}'
            'catch(e){console.error(e);}'
            'setTimeout(fd,200);'
            '}'
            'setInterval(fd,2500);'
            'window.onload=fd;'
        )
        return (
            '<!doctype html><html lang="pt"><head>'
            '<meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>MotoSeguro</title>'
            '<style>' + css + '</style>'
            '</head><body>'
            + body +
            '<script>' + js + '</script>'
            '</body></html>'
        )


# ===========================================================================
# SistemaMotocicleta
# ===========================================================================
class SistemaMotocicleta:
    ESTADO_NORMAL    = "NORMAL"
    ESTADO_LIMITANDO = "LIMITANDO"
    ESTADO_ROUBO     = "ROUBO"

    def __init__(self):
        print("\n=== MotoSeguro ===\n")
        self.gps     = ModuloGPS()
        self.gsm     = ModuloGSM()
        self.vel     = SensorVelocidade()
        self.ignicao = SensorIgnicao(IGNICAO_PIN)
        self.web     = WebDashboard(self)

        self.pin_atuador = Pin(ATUADOR_PIN, Pin.OUT)
        self.pin_atuador.value(0)
        self.pin_led = Pin(LED_PIN, Pin.OUT)
        self.pin_led.value(0)
        self.pin_buzzer = Pin(BUZZER_PIN, Pin.OUT)
        self.pin_buzzer.value(0)
        self.pin_botao = Pin(BOTAO_PIN, Pin.IN, Pin.PULL_UP)

        self.vel_kmh      = 0.0
        self.limite_ativo = LIMITE_VEL_PADRAO
        self.estado       = self.ESTADO_NORMAL
        self.gsm_ok       = False
        self._t_ultimo_sms = 0
        self._gc_counter   = 0

    async def iniciar(self):
        await self.web.iniciar()
        self.gsm_ok = await self.gsm.inicializar()
        if not self.gsm_ok:
            print("[SISTEMA] AVISO: GSM nao respondeu.")
        asyncio.create_task(self.task_controle_velocidade())
        asyncio.create_task(self.task_seguranca())
        asyncio.create_task(self.task_gps_gsm())
        asyncio.create_task(self.task_interface())
        asyncio.create_task(self.task_comandos_sms())
        asyncio.create_task(self.task_garbage_collection())
        print("[SISTEMA] Todas as tasks iniciadas.")
        while True:
            await asyncio.sleep(1)

    # ------------------------------------------------------------------
    async def task_controle_velocidade(self):
        while True:
            self.vel_kmh = self.vel.calcular()
            if self.estado != self.ESTADO_ROUBO:
                if self.vel_kmh > (self.limite_ativo + MARGEM_KMH):
                    self.pin_atuador.value(1)
                    self.estado = self.ESTADO_LIMITANDO
                elif self.vel_kmh <= self.limite_ativo:
                    self.pin_atuador.value(0)
                    self.estado = self.ESTADO_NORMAL
            await asyncio.sleep_ms(400)

    async def task_seguranca(self):
        while True:
            if (not self.ignicao.ligada and
                    self.vel_kmh > 5.0 and
                    self.estado != self.ESTADO_ROUBO):
                print("[SEGURANCA] ROUBO DETECTADO!")
                self.estado = self.ESTADO_ROUBO
                self.pin_atuador.value(0)
            if self.pin_botao.value() == 0:
                await asyncio.sleep_ms(100)
                if self.pin_botao.value() == 0:
                    print("[SEGURANCA] Reset manual.")
                    self.estado = self.ESTADO_NORMAL
                    self.pin_atuador.value(0)
            await asyncio.sleep(1)

    async def task_gps_gsm(self):
        while True:
            await self.gps.atualizar()
            agora = time.time()
            if (self.estado == self.ESTADO_ROUBO and
                    (agora - self._t_ultimo_sms) > SMS_INTERVALO_S and
                    self.gsm_ok):
                msg = "ALERTA ROUBO!\nVel: {:.1f} km/h\n{}".format(
                    self.vel_kmh, self.gps.obter_texto_sms())
                if await self.gsm.enviar_sms(NUMERO_PROPRIETARIO, msg):
                    self._t_ultimo_sms = agora
            await asyncio.sleep(2)

    async def task_comandos_sms(self):
        """Comandos SMS suportados:
          LIMIT:XX  — novo limite (ex: LIMIT:60)
          LIMIT:0   — repõe limite padrão
          STATUS    — responde com estado actual
          RESET     — cancela estado de roubo remotamente
        """
        await asyncio.sleep(15)
        while True:
            if self.gsm_ok:
                try:
                    for corpo in await self.gsm.ler_sms():
                        await self._processar_comando(corpo.upper().strip())
                except Exception as e:
                    print("[CMD] Erro: {}".format(e))
            await asyncio.sleep(30)

    async def _processar_comando(self, texto):
        print("[CMD] Recebido: {}".format(texto))
        if texto.startswith("LIMIT:"):
            try:
                v = int(texto[6:].strip())
                if v == 0:
                    self.limite_ativo = LIMITE_VEL_PADRAO
                    print("[CMD] Limite reposto: {} km/h".format(LIMITE_VEL_PADRAO))
                elif 10 <= v <= 200:
                    self.limite_ativo = v
                    print("[CMD] Novo limite: {} km/h".format(v))
            except ValueError:
                print("[CMD] Valor LIMIT invalido.")
        elif texto == "STATUS" or "Status" or "Estado" or "ESTADO":
            r = "Estado: {}\nVel: {:.1f} km/h\nLimite: {} km/h\n{}".format(
                self.estado, self.vel_kmh, self.limite_ativo,
                self.gps.obter_texto_sms())
            await self.gsm.enviar_sms(NUMERO_PROPRIETARIO, r)
        elif texto == "RESET" or "Reset":
            if self.estado == self.ESTADO_ROUBO:
                self.estado = self.ESTADO_NORMAL
                self.pin_atuador.value(0)
                print("[CMD] Roubo cancelado por SMS.")
                await self.gsm.enviar_sms(NUMERO_PROPRIETARIO,
                                          "Sistema resetado com sucesso.")

    async def task_interface(self):
        led_val = 0
        while True:
            if self.estado == self.ESTADO_NORMAL:
                self.pin_led.value(0)
                self.pin_buzzer.value(0)
                await asyncio.sleep(1)
                continue
            led_val = 1 - led_val
            if self.estado == self.ESTADO_ROUBO:
                self.pin_led.value(led_val)
                self.pin_buzzer.value(led_val)
                await asyncio.sleep_ms(200)
            else:
                # Limitando: só LED pisca, sem barulho
                self.pin_led.value(led_val)
                self.pin_buzzer.value(0)
                await asyncio.sleep_ms(600)

    async def task_garbage_collection(self):
        """Força garbage collection periodicamente para evitar fragmentação."""
        while True:
            await asyncio.sleep(30)
            gc.collect()
            print("[GC] Garbage collection realizado.")


# ===========================================================================
# Ponto de entrada
# ===========================================================================
def main():
    gc.collect()
    time.sleep_ms(1000)
    app = SistemaMotocicleta()
    try:
        asyncio.run(app.iniciar())
    except KeyboardInterrupt:
        print("Parado.")
    finally:
        Pin(ATUADOR_PIN, Pin.OUT).value(0)


if __name__ == "__main__":
    main()
