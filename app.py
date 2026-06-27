import os
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, Response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import requests
import json
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime, timedelta, timezone

load_dotenv()
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.getenv('SECRET_KEY', 'cambia-esta-clave-larga-123456789')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///cuidador.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
scheduler = BackgroundScheduler(daemon=True)

def revisar_y_enviar_recordatorios():
    print("Revisando recordatorios...")
    with app.app_context():
        ahora = datetime.now(timezone(timedelta(hours=-3)))
        en_15_min = ahora + timedelta(minutes=15)
        pacientes = Paciente.query.join(Cuidador).filter(Cuidador.telefono!= None).all()
        for p in pacientes:
            for h in p.historias:
                if "Turno agendado:" in h.nota and "| FECHA:" in h.nota and " | ENVIADO" not in h.nota:
                    try:
                        fecha_str = h.nota.split("| FECHA:")[1].strip()
                        fecha_turno = datetime.fromisoformat(fecha_str)
                        if 0 <= (fecha_turno - ahora).total_seconds() <= 900:
                            titulo = h.nota.split(":")[1].split("-")[0].strip()
                            mensaje = preguntar_ia(titulo, "")
                            if enviar_whatsapp(mensaje, p.cuidador.telefono):
                                h.nota = h.nota + " | ENVIADO"
                                db.session.commit()
                                print("Recordatorio enviado a " + p.nombre)
                    except Exception as e:
                        print("Error historia " + str(h.id) + ": " + str(e))

scheduler.add_job(revisar_y_enviar_recordatorios, 'interval', minutes=1)
scheduler.start()
print("✅ Scheduler iniciado - revisa cada 1 min")

@app.route("/", methods=["GET"])
def home():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route("/webhook", methods=["POST"])
def webhook():
    msg = request.form.get('Body')
    resp = MessagingResponse()
    resp.message(f"Recibido: {msg} ✅")
    return str(resp)

twilio_client = None
TWILIO_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
if TWILIO_SID and TWILIO_TOKEN:
    twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
    print("✅ Twilio conectado")
else:
    print("⚠️ Twilio sin credenciales")

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    google_token = db.Column(db.String(500))  # <-- AGREGAR
    google_refresh_token = db.Column(db.String(500))  # <-- AGREGAR
    cuidador = db.relationship('Cuidador', backref='user', uselist=False)

class Cuidador(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    matricula = db.Column(db.String(50))
    telefono = db.Column(db.String(20))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))

class Paciente(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    edad = db.Column(db.Integer)
    dni = db.Column(db.String(20))
    direccion = db.Column(db.String(200))
    obrasocial = db.Column(db.String(100))
    tel_emergencia1 = db.Column(db.String(20), nullable=False)
    tel_emergencia2 = db.Column(db.String(20))
    tel_emergencia3 = db.Column(db.String(20))
    cuidador_id = db.Column(db.Integer, db.ForeignKey('cuidador.id'))
    historias = db.relationship('Historia', backref='paciente', lazy=True, order_by='Historia.fecha.desc()')
    medicamentos = db.relationship('Medicamento', backref='paciente', lazy=True)

class Historia(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
    nota = db.Column(db.Text, nullable=False)
    paciente_id = db.Column(db.Integer, db.ForeignKey('paciente.id'), nullable=False)

class Medicamento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    dosis = db.Column(db.String(50))
    hora = db.Column(db.String(5))
    paciente_id = db.Column(db.Integer, db.ForeignKey('paciente.id'), nullable=False)
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

SCOPES = ['https://www.googleapis.com/auth/calendar.events']

def get_credentials():
    if not current_user.is_authenticated:
        return None
    
    if not current_user.google_token:
        return None
    
    creds = Credentials(
        token=current_user.google_token,
        refresh_token=current_user.google_refresh_token,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=json.loads(os.getenv('GOOGLE_CREDENTIALS_JSON'))['web']['client_id'],
        client_secret=json.loads(os.getenv('GOOGLE_CREDENTIALS_JSON'))['web']['client_secret'],
        scopes=SCOPES
    )
    
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        current_user.google_token = creds.token
        db.session.commit()
    
    return creds

def limpiar_tel(tel):
    return tel.replace("+549","").replace("+54","").replace(" ","").replace("-","")

def enviar_whatsapp(mensaje, telefono):
    global twilio_client
    if not twilio_client:
        print("ERROR: Twilio no configurado")
        return False
    try:
        num_limpio = limpiar_tel(telefono)
        num_destino = f'whatsapp:+549{num_limpio}'
        from_num = os.getenv('TWILIO_NUMERO')
        print(f"Enviando WhatsApp a {num_destino}...")
        message = twilio_client.messages.create(from_=from_num, body=mensaje, to=num_destino)
        print(f"✅ Mensaje Twilio enviado: {message.sid}")
        return True
    except Exception as e:
        print(f"❌ Error Twilio: {e}")
        return False

def preguntar_ia(titulo, descripcion):
    prompt = f"Generá un recordatorio amigable para WhatsApp sobre este turno: {titulo}. {descripcion}. Que suene natural, como si fuera de un cuidador."
    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"}, json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": prompt}]}, timeout=10)
        data = r.json()
        if 'choices' in data and len(data['choices']) > 0:
            return data['choices'][0]['message']['content']
        else:
            return f"⏰ Recordatorio: Tenés '{titulo}' en 15 minutos. {descripcion}"
    except Exception as e:
        print(f"Error IA: {e}")
        return f"⏰ Recordatorio: Tenés '{titulo}' en 15 minutos. {descripcion}"

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        email = request.form['email']
        password = generate_password_hash(request.form['password'])
        if User.query.filter_by(email=email).first():
            flash('Email ya registrado')
            return redirect(url_for('registro'))
        user = User(email=email, password=password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('datos_cuidador'))
    html = '<div class="logo-title"><div class="logo-shield"><div class="heart"></div></div><h1 style="margin:0;color:white;font-size:42px;text-shadow:0 3px 6px rgba(0,0,0,0.3)">careMates</h1></div><p style="text-align:center;color:white;margin-bottom:25px;font-size:16px;font-weight:600">Tu Red de Apoyo y Confianza</p><h2 style="text-align:center;margin-bottom:25px;color:var(--verde)">Registro Cuidador</h2><form method="POST"><input name="email" type="email" placeholder="Email" required><input name="password" type="password" placeholder="Contraseña" required><button>Crear cuenta</button></form><p style="text-align:center;margin-top:20px">¿Ya tenés cuenta? <a href="/login">Entrar</a></p>'
    return render_template_string(BASE, content=html)

@app.route('/conectar_google')
@login_required
def conectar_google():
    import json
    import os
    from google_auth_oauthlib.flow import Flow
    
    SCOPES = ['https://www.googleapis.com/auth/calendar.events']
    
    # Cargar credenciales desde variable de entorno
    creds_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
    creds_data = json.loads(creds_json)
    
    flow = Flow.from_client_config(
        creds_data, 
        scopes=SCOPES, 
        redirect_uri=url_for('oauth_callback', _external=True, _scheme='https')
    )
    auth_url, _ = flow.authorization_url(access_type='offline', prompt='consent')
    return redirect(auth_url)

@app.route('/oauth_callback')
@login_required
def oauth_callback():
    
    SCOPES = ['https://www.googleapis.com/auth/calendar.events']
    
    # Cargar credenciales desde variable de entorno
    creds_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
    creds_data = json.loads(creds_json)
    
    flow = Flow.from_client_config(
        creds_data, 
        scopes=SCOPES, 
        redirect_uri=url_for('oauth_callback', _external=True, _scheme='https')
    )
    flow.fetch_token(authorization_response=request.url)
    
    creds = flow.credentials
    # Guardar token en DB
    current_user.google_token = creds.token
    current_user.google_refresh_token = creds.refresh_token
    db.session.commit()
    
    flash('Google Calendar conectado ✅')
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form['email']).first()
        if user and check_password_hash(user.password, request.form['password']):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Email o contraseña incorrecta')
    html = '<div class="logo-title"><div class="logo-shield"><div class="heart"></div></div><h1 style="margin:0;color:white;font-size:42px;text-shadow:0 3px 6px rgba(0,0,0,0.3)">careMates</h1></div><p style="text-align:center;color:white;margin-bottom:25px;font-size:16px;font-weight:600">Tu Red de Apoyo y Confianza</p><h2 style="text-align:center;margin-bottom:25px;color:var(--verde)">Ingresar</h2><form method="POST"><input name="email" type="email" placeholder="Email" required><input name="password" type="password" placeholder="Contraseña" required><button>Entrar</button></form><p style="text-align:center;margin-top:20px">¿No tenés cuenta? <a href="/registro">Registrate acá</a></p>'
    return render_template_string(BASE, content=html)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))
@app.route('/manifest.json')
def manifest():
    manifest_data = {"name": "careMates - Tu Red de Apoyo", "short_name": "careMates", "description": "Gestión de pacientes y turnos para cuidadores", "start_url": "/login", "display": "standalone", "background_color": "#2D7D7D", "theme_color": "#4FB3C7", "orientation": "portrait", "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"}, {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}]}
    return Response(json.dumps(manifest_data), mimetype='application/json')

@app.route('/service-worker.js')
def sw():
    sw_code = """self.addEventListener('install', e => {
  e.waitUntil(caches.open('caremates-v2').then(cache => {
    return cache.addAll([
      '/',
      '/login',
      '/registro',
      '/manifest.json',
      '/static/icon-192.png',
      '/static/icon-512.png'
    ])
  }))
});

self.addEventListener('fetch', e => {
  e.respondWith(
    caches.match(e.request).then(resp => {
      return resp || fetch(e.request).catch(() => {
        return caches.match('/login')
      })
    })
  )
});"""
    return Response(sw_code, mimetype='application/javascript')

@app.route('/datos_cuidador', methods=['GET', 'POST'])
@login_required
def datos_cuidador():
    if current_user.cuidador:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        c = Cuidador(nombre=request.form['nombre'], matricula=request.form['matricula'], telefono=limpiar_tel(request.form['telefono']), user_id=current_user.id)
        db.session.add(c)
        db.session.commit()
        return redirect(url_for('conectar_google'))
    html = '<h2>Datos del Cuidador</h2><form method="POST"><input name="nombre" placeholder="Nombre completo" required><input name="matricula" placeholder="Matrícula"><input name="telefono" placeholder="Tu WhatsApp sin +549 ej: 3884123456" required><button>Guardar</button></form>'
    return render_template_string(BASE, content=html)

@app.route('/cuidador/editar', methods=['GET', 'POST'])
@login_required
def editar_cuidador():
    c = current_user.cuidador
    if request.method == 'POST':
        c.nombre = request.form['nombre']
        c.matricula = request.form['matricula']
        c.telefono = limpiar_tel(request.form['telefono'])
        db.session.commit()
        flash('Datos actualizados correctamente')
        return redirect(url_for('dashboard'))
    html = f'<h2>Editar Mis Datos</h2><form method="POST"><input name="nombre" value="{c.nombre}" placeholder="Nombre completo" required><input name="matricula" value="{c.matricula or ""}" placeholder="Matrícula"><input name="telefono" value="{c.telefono or ""}" placeholder="Tu WhatsApp sin +549 ej: 3884123456" required><button>Guardar Cambios</button></form>'
    return render_template_string(BASE, content=html)

@app.route('/dashboard')
@login_required
def dashboard():
    if not current_user.cuidador:
        return redirect(url_for('datos_cuidador'))
    creds = get_credentials()
    if not creds:
        html = '<h2>Conectá Google Calendar</h2><p>Para agendar turnos necesitás conectar tu cuenta</p><a href="/conectar_google"><button>Conectar Google</button></a>'
        return render_template_string(BASE, content=html)
    pacientes = Paciente.query.filter_by(cuidador_id=current_user.cuidador.id).all()
    lista = ''.join([f'<div class="paciente-item" onclick="location.href=\'/paciente/{p.id}\'"><b>{p.nombre}</b> - {p.edad} años - DNI {p.dni}</div>' for p in pacientes])
    html = f'<h2>Mis Pacientes</h2>{lista if lista else "<p>No hay pacientes cargados</p>"}<a href="/paciente/nuevo"><button>+ Nuevo Paciente</button></a>'
    return render_template_string(BASE, content=html)

@app.route('/paciente/nuevo', methods=['GET', 'POST'])
@login_required
def nuevo_paciente():
    if request.method == 'POST':
        p = Paciente(nombre=request.form['nombre'], edad=request.form['edad'], dni=request.form['dni'], direccion=request.form.get('direccion'), obrasocial=request.form.get('obrasocial'), tel_emergencia1=limpiar_tel(request.form['tel1']), tel_emergencia2=limpiar_tel(request.form['tel2']), tel_emergencia3=limpiar_tel(request.form['tel3']), cuidador_id=current_user.cuidador.id)
        db.session.add(p)
        db.session.commit()
        flash('Paciente creado correctamente')
        return redirect(url_for('dashboard'))
    html = '<h2>Nuevo Paciente</h2><form method="POST"><input name="nombre" placeholder="Nombre completo" required><div class="grid"><input name="edad" type="number" placeholder="Edad"><input name="dni" placeholder="DNI"></div><input name="direccion" placeholder="Dirección"><input name="obrasocial" placeholder="Obra Social"><h4>Contactos de Emergencia</h4><input name="tel1" placeholder="Tel 1 sin +549" required><input name="tel2" placeholder="Tel 2 sin +549"><input name="tel3" placeholder="Tel 3 sin +549"><button>Guardar Paciente</button></form>'
    return render_template_string(BASE, content=html)

@app.route('/paciente/<int:id>/editar', methods=['GET', 'POST'])
@login_required
def editar_paciente(id):
    p = Paciente.query.get_or_404(id)
    if p.cuidador_id!= current_user.cuidador.id:
        return "No autorizado", 403
    if request.method == 'POST':
        p.nombre = request.form['nombre']
        p.edad = request.form['edad']
        p.dni = request.form['dni']
        p.direccion = request.form.get('direccion')
        p.obrasocial = request.form.get('obrasocial')
        p.tel_emergencia1 = limpiar_tel(request.form['tel1'])
        p.tel_emergencia2 = limpiar_tel(request.form['tel2'])
        p.tel_emergencia3 = limpiar_tel(request.form['tel3'])
        db.session.commit()
        flash('Datos actualizados correctamente')
        return redirect(url_for('ver_paciente', id=id))
    html = f'<h2>Editar {p.nombre}</h2><form method="POST"><input name="nombre" value="{p.nombre}" required><div class="grid"><input name="edad" type="number" value="{p.edad}"><input name="dni" value="{p.dni}"></div><input name="direccion" value="{p.direccion or ""}"><input name="obrasocial" value="{p.obrasocial or ""}"><h4>Contactos de Emergencia</h4><input name="tel1" value="{p.tel_emergencia1}" required><input name="tel2" value="{p.tel_emergencia2 or ""}"><input name="tel3" value="{p.tel_emergencia3 or ""}"><button>Guardar Cambios</button></form>'
    return render_template_string(BASE, content=html)
@app.route('/paciente/<int:id>')
@login_required
def ver_paciente(id):
    p = Paciente.query.get_or_404(id)
    if p.cuidador_id!= current_user.cuidador.id:
        return "No autorizado", 403
    historias = ''.join([f'<div class="historia-item"><b>{(h.fecha - timedelta(hours=3)).strftime("%d/%m %Y %H:%M")}</b><br>{h.nota}</div>' for h in p.historias])
    meds = ''.join([f'<div class="med-item">💊 {m.nombre} - {m.dosis} - {m.hora}hs</div>' for m in p.medicamentos])
    html = f'<h2>{p.nombre}</h2><p><b>Edad:</b> {p.edad} años | <b>DNI:</b> {p.dni}</p><p><b>Dirección:</b> {p.direccion or "-"} | <b>Obra Social:</b> {p.obrasocial or "-"}</p><div class="grid"><a href="/paciente/{id}/editar"><button class="secondary">✏️ Editar Datos</button></a><a href="/paciente/{id}/historia/nueva"><button>📝 Editar Historia</button></a><a href="/paciente/{id}/turno"><button>📅 Agendar Turno</button></a><a href="/paciente/{id}/medicamento"><button>💊 Registrar Medicamento</button></a><a href="/paciente/{id}/sos"><button class="danger">🚨 SOS Emergencia</button></a><button class="ia" onclick="abrirIA()">🤖 Consultar IA</button></div><h3>Medicamentos Programados</h3>{meds if meds else "<p>No hay medicamentos registrados</p>"}<h3>Historia Clínica</h3>{historias if historias else "<p>Sin registros aún</p>"}'
    return render_template_string(BASE, content=html)

@app.route('/paciente/<int:id>/historia/nueva', methods=['GET', 'POST'])
@login_required
def nueva_historia(id):
    p = Paciente.query.get_or_404(id)
    if request.method == 'POST':
        h = Historia(nota=request.form['nota'], paciente_id=id)
        db.session.add(h)
        db.session.commit()
        flash('Nota agregada a la historia clínica')
        return redirect(url_for('ver_paciente', id=id))
    html = f'<h2>Nueva Nota - {p.nombre}</h2><form method="POST"><textarea name="nota" rows="8" required></textarea><button>Guardar Nota</button></form>'
    return render_template_string(BASE, content=html)

@app.route('/paciente/<int:id>/turno', methods=['GET', 'POST'])
@login_required
def agendar_turno(id):
    p = Paciente.query.get_or_404(id)
    if request.method == 'POST':
        titulo = request.form['titulo'].strip()
        desc = request.form.get('desc', '').strip()
        fecha_dt = datetime.fromisoformat(request.form['fecha'])
        fecha_fin = fecha_dt + timedelta(hours=1)
        evento = {'summary': f'{p.nombre} - {titulo}', 'description': desc, 'start': {'dateTime': fecha_dt.isoformat(), 'timeZone': 'America/Argentina/Jujuy'}, 'end': {'dateTime': fecha_fin.isoformat(), 'timeZone': 'America/Argentina/Jujuy'}}
        creds = get_credentials()
        service = build('calendar', 'v3', credentials=creds)
        service.events().insert(calendarId='primary', body=evento).execute()
        h = Historia(nota=f"Turno agendado: {titulo} - {desc} | FECHA:{fecha_dt.isoformat()}", paciente_id=id)
        db.session.add(h)
        db.session.commit()
        hora_aviso = fecha_dt - timedelta(minutes=15)
        mensaje_ia = preguntar_ia(titulo, desc)
        telefono_cuidador = current_user.cuidador.telefono
        flash(f'Turno agendado. Te aviso 15 min antes por WhatsApp')
        return redirect(url_for('ver_paciente', id=id))
    html = f'<h2>Agendar Turno para {p.nombre}</h2><form method="POST"><input name="titulo" placeholder="Título" required><input name="fecha" type="datetime-local" required><textarea name="desc"></textarea><button>Agendar Turno</button></form>'
    return render_template_string(BASE, content=html)

@app.route('/paciente/<int:id>/medicamento', methods=['GET', 'POST'])
@login_required
def registrar_medicamento(id):
    p = Paciente.query.get_or_404(id)
    if request.method == 'POST':
        m = Medicamento(nombre=request.form['nombre'], dosis=request.form['dosis'], hora=request.form['hora'], paciente_id=id)
        db.session.add(m)
        db.session.commit()
        flash('Medicamento registrado')
        return redirect(url_for('ver_paciente', id=id))
    html = f'<h2>Medicamento para {p.nombre}</h2><form method="POST"><input name="nombre" placeholder="Nombre" required><div class="grid"><input name="dosis" placeholder="Dosis"><input name="hora" type="time" required></div><button>Guardar</button></form>'
    return render_template_string(BASE, content=html)

@app.route('/paciente/<int:id>/sos')
@login_required
def sos(id):
    p = Paciente.query.get_or_404(id)
    mensaje = f'🚨 EMERGENCIA: {p.nombre} DNI {p.dni} necesita asistencia urgente. Contactar {current_user.cuidador.nombre}.'
    for tel in [p.tel_emergencia1, p.tel_emergencia2, p.tel_emergencia3]:
        if tel:
            enviar_whatsapp(mensaje, tel)
    flash(f'🚨 SOS enviado a todos los contactos')
    return redirect(url_for('ver_paciente', id=id))

@app.route('/consultar_ia', methods=['POST'])
@login_required
def consultar_ia():
    pregunta = request.form.get('pregunta')
    if not pregunta:
        return "Escribí una pregunta"
    prompt = f"Sos un asistente médico para cuidadores. Responde claro: {pregunta}. No diagnostico, solo orientación."
    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"}, json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": prompt}]}, timeout=10)
        data = r.json()
        if 'choices' in data and len(data['choices']) > 0:
            respuesta = data['choices'][0]['message']['content']
            return f"{respuesta}\n\n⚠️ Ante emergencias llamá al 107"
        else:
            return "La IA no respondió bien. Probá de nuevo."
    except Exception as e:
        print(f"Error IA ruta: {e}")
        return "No pude consultar la IA ahora."

CSS = '''<style>:root{--verde:#2D7D7D;--celeste:#4FB3C7;--rojo:#E74C3C;--gris-claro:#F5F7FA;--gris-oscuro:#2C3E50}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;max-width:800px;margin:40px auto;padding:20px;background:linear-gradient(135deg, var(--celeste) 0%, var(--verde) 100%);color:var(--gris-oscuro);min-height:100vh}.card{background:white;padding:35px;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,0.15);margin-bottom:20px;border-top:5px solid var(--rojo)}input,textarea,button,select{width:100%;padding:14px;margin:10px 0;border:2px solid #E0E0E0;border-radius:10px;font-size:15px;box-sizing:border-box;transition:0.3s}input:focus,textarea:focus{border-color:var(--celeste);outline:none;box-shadow:0 0 0 3px rgba(79,179,199,0.1)}button{background:linear-gradient(135deg, var(--verde) 0%, var(--celeste) 100%);color:white;border:none;cursor:pointer;font-weight:700;font-size:16px;border-radius:10px;transition:0.3s}button:hover{transform:translateY(-2px);box-shadow:0 4px 12px rgba(45,125,125,0.3)}button.danger{background:linear-gradient(135deg, var(--rojo) 0%, #C0392B 100%)}button.secondary{background:#95A5A6}button.ia{background:linear-gradient(135deg, #9146ff 0%, #6B46C1 100%)}a{color:var(--verde);text-decoration:none;font-weight:600}.nav{display:flex;gap:15px;margin-bottom:20px;flex-wrap:wrap}.nav a{padding:10px 18px;background:white;border-radius:10px;color:var(--verde);box-shadow:0 2px 6px rgba(0,0,0,0.1)}.paciente-item{padding:18px;border-bottom:2px solid var(--gris-claro);cursor:pointer;border-radius:10px;transition:0.2s}.paciente-item:hover{background:var(--gris-claro);border-left:4px solid var(--celeste)}.historia-item{padding:14px;border-left:4px solid var(--verde);margin:12px 0;background:var(--gris-claro);border-radius:6px}.med-item{padding:12px;background:rgba(79,179,199,0.1);border-left:4px solid var(--celeste);border-radius:8px;margin:8px 0;font-size:14px}.flash{background:rgba(45,125,125,0.1);padding:14px;border-radius:10px;margin:15px 0;border-left:4px solid var(--verde);color:var(--verde);font-weight:600}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}h2,h3,h4{margin-top:0;color:var(--verde)}.logo-title{display:flex;align-items:center;justify-content:center;gap:15px;margin-bottom:25px}.logo-shield{width:60px;height:60px;background:linear-gradient(135deg, var(--celeste) 0%, var(--verde) 100%);border-radius:50% 50% 50% 50% / 60% 60% 40% 40%;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 12px rgba(0,0,0,0.2)}.heart{width:28px;height:28px;background:var(--rojo);transform:rotate(45deg);border-radius:5px 5px 50% 50%;position:relative}.heart:before,.heart:after{content:"";width:28px;height:28px;position:absolute;background:var(--rojo);border-radius:50%}.heart:before{top:-14px;left:0}.heart:after{left:-14px;top:0}#modalIA{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:999;align-items:center;justify-content:center;padding:20px}#modalIA.modal-content{position:relative;background:white;padding:25px;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,0.3);max-width:500px;width:100%;max-height:90vh;border-top:5px solid var(--rojo);overflow-y:auto;box-sizing:border-box}#respuestaIA{margin-top:15px;padding:12px;background:#f8f9fa;border-radius:8px;max-height:40vh;overflow-y:auto;white-space:pre-wrap;line-height:1.5;font-size:14px;box-sizing:border-box}@media(max-width:600px){.grid{grid-template-columns:1fr}}</style><link rel="manifest" href="/manifest.json"><meta name="theme-color" content="#4FB3C7">'''

BASE = CSS + '''<div class="nav"><a href="{{ url_for('dashboard') }}">🏠 Pacientes</a>{% if current_user.is_authenticated %}<a href="{{ url_for('editar_cuidador') }}">⚙️ Mis Datos</a><span style="margin-left:auto">Hola {{ current_user.cuidador.nombre if current_user.cuidador else current_user.email }}</span><a href="{{ url_for('logout') }}">Salir</a>{% endif %}</div>{% with messages = get_flashed_messages() %}{% if messages %}{% for msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}{% endif %}{% endwith %}<div class="card">{{ content|safe }}</div><div id="modalIA" onclick="cerrarIA()"><div class="modal-content" onclick="event.stopPropagation()"><h3>🤖 Consultar IA Médica</h3><textarea id="preguntaIA" rows="4" placeholder="Ej: ¿Qué hago si tiene fiebre 38.5?"></textarea><button class="ia" onclick="consultarIA()">Preguntar</button><button class="secondary" onclick="cerrarIA()">Cerrar</button><div id="respuestaIA" style="display:none"></div></div></div><script>function abrirIA(){document.getElementById('modalIA').style.display='flex';document.getElementById('respuestaIA').style.display='none';document.getElementById('preguntaIA').value=''}function cerrarIA(){document.getElementById('modalIA').style.display='none'}function consultarIA(){let pregunta=document.getElementById('preguntaIA').value;if(!pregunta)return;let respDiv=document.getElementById('respuestaIA');respDiv.style.display='block';respDiv.innerText='Consultando...';let form=new FormData();form.append('pregunta',pregunta);fetch('/consultar_ia',{method:'POST',body:form}).then(r=>r.text()).then(data=>{respDiv.innerText=data});}if('serviceWorker' in navigator){navigator.serviceWorker.register('/service-worker.js')}</script>'''

with app.app_context():
    db.create_all()
    print("✅ Base de datos lista - Tablas creadas")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
