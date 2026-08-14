import streamlit as st
import pandas as pd
import requests
import contextlib
import io
import os
import time
from datetime import datetime, timedelta, date
from sqlalchemy import create_engine, text, bindparam

# ==============================================================================
# CONFIGURATION PAGE STREAMLIT
# ==============================================================================
st.set_page_config(
    page_title="Gestion Énergétique - Multi-Compteurs",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- INJECTION CSS DESIGN ---
st.markdown("""
<style>
    .stApp { background-color: #f1f5f9; }
    .kpi-card {
        background-color: #ffffff;
        border-radius: 12px;
        padding: 18px;
        border-left: 6px solid #2563eb;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
        margin-bottom: 12px;
    }
    .kpi-card-danger {
        border-left: 6px solid #dc2626 !important;
        background-color: #fef2f2;
    }
    .kpi-title { font-size: 0.85rem; color: #64748b; font-weight: 600; text-transform: uppercase; }
    .kpi-value { font-size: 1.8rem; font-weight: 700; color: #0f172a; margin-top: 4px; }
    .main-header {
        background: linear-gradient(90deg, #1e293b 0%, #334155 100%);
        color: white; padding: 18px 24px;
        border-radius: 12px; margin-bottom: 24px;
    }
</style>
""", unsafe_allow_html=True)

# ==============================================================================
# AUTHENTIFICATION GLOBALE (EXPIRATION GLISSANTE 20 MINUTES)
# ==============================================================================
SESSION_TIMEOUT_SECONDS = 20 * 60

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
if "last_activity" not in st.session_state:
    st.session_state["last_activity"] = time.time()

def check_password():
    current_time = time.time()
    
    # 1. Vérification du délai d'inactivité
    if st.session_state["authenticated"]:
        time_elapsed = current_time - st.session_state.get("last_activity", current_time)
        if time_elapsed > SESSION_TIMEOUT_SECONDS:
            st.session_state["authenticated"] = False
            st.session_state["admin_authenticated"] = False
            st.warning("⏱️ Votre session a expiré après 20 minutes d'inactivité. Veuillez vous reconnecter.")
            return False
        st.session_state["last_activity"] = current_time
        return True

    # 2. Écran de connexion principal
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown('<div class="main-header" style="text-align: center;"><h2>🔒 Accès Sécurisé</h2><span>Suivi Énergétique du Parc Municipal</span></div>', unsafe_allow_html=True)
        expected_pwd = st.secrets.get("APP_PASSWORD")
        if not expected_pwd:
            st.error("⚠️ Aucun mot de passe (APP_PASSWORD) n'est configuré dans les secrets de l'application. Accès bloqué.")
            st.stop()
            
        with st.form("form_login"):
            pwd_input = st.text_input("Veuillez saisir le mot de passe d'accès :", type="password")
            submit_login = st.form_submit_button("Se connecter", type="primary", use_container_width=True)
            if submit_login:
                if pwd_input == expected_pwd:
                    st.session_state["authenticated"] = True
                    st.session_state["last_activity"] = time.time()
                    st.rerun()
                else:
                    st.error("🔑 Mot de passe incorrect.")
    return False

if not check_password():
    st.stop()

# ==============================================================================
# NOTIFICATIONS FLASH EN SESSION
# ==============================================================================
if "flash_msg" not in st.session_state:
    st.session_state["flash_msg"] = None

def set_flash(msg: str, level: str = "success"):
    st.session_state["flash_msg"] = (level, msg)

def display_flash():
    if st.session_state.get("flash_msg"):
        level, msg = st.session_state.pop("flash_msg")
        if level == "success":
            st.success(msg, icon="✅")
        elif level == "error":
            st.error(msg, icon="🚨")
        elif level == "info":
            st.info(msg, icon="ℹ️")

# ==============================================================================
# CONSTANTES ET RÉFÉRENTIELS (TOUS FLUIDES INCLUS)
# ==============================================================================
DEFAULT_SECTEURS = [
    "Secteur 1 - Centre / Administratif",
    "Secteur 2 - Nord / Enseignement",
    "Secteur 3 - Sud / Écoles & Petite Enfance",
    "Secteur 4 - Est / Sport & Loisirs",
    "Secteur 5 - Ouest / Culture & Patrimoine",
    "Secteur 6 - Technique & Logistique",
    "Secteur 7 - Social & Santé"
]

REFERENTIEL_EPOQUES = {
    "1960-1974 (Avant RT)": 350, "1975-1981 (RT 1974)": 220,
    "1982-1988 (RT 1982)": 160, "1989-2000 (RT 1988/2000)": 110,
    "2001-2012 (RT 2005)": 75, "2013-2021 (RT 2012)": 50, "2022+ (RE 2020)": 30
}

DELAI_DJU_JOURS = 5

UNITES_PAR_ENERGIE = {
    "Gaz naturel": ["m3", "kWh", "MWh"],
    "Chauffage urbain": ["kWh", "MWh"],
    "Électricité": ["kWh", "MWh"],
    "Eau froide": ["m3"],
    "ECS": ["m3", "kWh", "MWh"],
    "Eau glacée": ["kWh", "MWh", "m3"],
}

LISTE_TYPES_ENERGIE = list(UNITES_PAR_ENERGIE.keys())
DJU_ANNUEL_REFERENCE = 2010

# ==============================================================================
# CONNEXION BDD ET INITIALISATION TABLES
# ==============================================================================
@st.cache_resource
def get_db_engine():
    if "db" in st.secrets and "url" in st.secrets["db"]:
        db_url = st.secrets["db"]["url"]
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return create_engine(
            db_url,
            pool_pre_ping=True,
            pool_size=20,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
        )
    else:
        return create_engine("sqlite:///parc_energie_multi_compteurs.db")

engine = get_db_engine()

@st.cache_resource
def init_db():
    is_postgres = "postgresql" in str(engine.url)
    pk_auto = "SERIAL PRIMARY KEY" if is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS secteurs (id {pk_auto}, nom VARCHAR(255) UNIQUE NOT NULL);"))
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS sites (id {pk_auto}, nom VARCHAR(255) UNIQUE NOT NULL, secteur VARCHAR(255) NOT NULL, surface_m2 FLOAT NOT NULL, epoque VARCHAR(255) NOT NULL, ordre INT DEFAULT 0);"))
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS compteurs (id {pk_auto}, site_id INT NOT NULL, numero_compteur VARCHAR(255) UNIQUE NOT NULL, type_energie VARCHAR(255) NOT NULL, unite VARCHAR(255) NOT NULL, FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE);"))
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS releves (id {pk_auto}, compteur_id INT NOT NULL, semaine_label VARCHAR(255) NOT NULL, date_releve DATE NOT NULL, conso_val FLOAT NOT NULL, dju_reels FLOAT NOT NULL, FOREIGN KEY (compteur_id) REFERENCES compteurs(id) ON DELETE CASCADE);"))
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS releves_audit (id {pk_auto}, releve_id INT NOT NULL, compteur_id INT NOT NULL, semaine_label VARCHAR(255) NOT NULL, ancienne_valeur FLOAT NOT NULL, nouvelle_valeur FLOAT NOT NULL, date_modification VARCHAR(255) NOT NULL);"))

    with engine.begin() as conn:
        try: conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_releves_compteur_semaine ON releves(compteur_id, semaine_label);"))
        except Exception: pass
    with engine.begin() as conn:
        try: conn.execute(text("CREATE INDEX IF NOT EXISTS idx_releves_compteur_date ON releves(compteur_id, date_releve);"))
        except Exception: pass

    if is_postgres:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE releves ADD COLUMN IF NOT EXISTS dju_fiable BOOLEAN DEFAULT TRUE"))
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE releves_audit ADD COLUMN IF NOT EXISTS champ_modifie VARCHAR(50) DEFAULT 'consommation'"))
    else:
        with engine.begin() as conn:
            try: conn.execute(text("ALTER TABLE releves ADD COLUMN dju_fiable BOOLEAN DEFAULT 1"))
            except Exception: pass
        with engine.begin() as conn:
            try: conn.execute(text("ALTER TABLE releves_audit ADD COLUMN champ_modifie VARCHAR(50) DEFAULT 'consommation'"))
            except Exception: pass

    with engine.begin() as conn:
        res = conn.execute(text("SELECT COUNT(*) FROM secteurs")).scalar()
        if res == 0:
            for s in DEFAULT_SECTEURS:
                conn.execute(text("INSERT INTO secteurs (nom) VALUES (:nom) ON CONFLICT DO NOTHING;"), {"nom": s})

init_db()

# ==============================================================================
# FONCTIONS DE REQUÊTES EN CACHE
# ==============================================================================
@st.cache_data(ttl=300, show_spinner=False)
def get_secteurs_list():
    with engine.connect() as conn:
        df = pd.read_sql(text("SELECT nom FROM secteurs ORDER BY id"), conn)
    return df['nom'].tolist() if not df.empty else DEFAULT_SECTEURS

@st.cache_data(ttl=300, show_spinner=False)
def get_compteurs_par_secteur(secteur_filtre):
    with engine.connect() as conn:
        query = """
            SELECT c.id as compteur_id, s.nom as "Bâtiment", s.secteur as "Secteur", s.ordre as "Ordre",
                   c.numero_compteur as "N° Compteur", c.type_energie as "Énergie", c.unite as "Unité"
            FROM compteurs c JOIN sites s ON c.site_id = s.id
        """
        if secteur_filtre != "Tous les secteurs":
            query += " WHERE s.secteur = :sec ORDER BY s.ordre ASC, s.nom ASC, c.numero_compteur ASC"
            return pd.read_sql(text(query), conn, params={"sec": secteur_filtre})
        else:
            query += " ORDER BY s.ordre ASC, s.nom ASC, c.numero_compteur ASC"
            return pd.read_sql(text(query), conn)

@st.cache_data(ttl=120, show_spinner=False)
def get_releves_s1_et_actuels(compteur_ids_tuple, date_d_str, semaine_label):
    if not compteur_ids_tuple:
        return {}, {}
    with engine.connect() as conn:
        df_prev = pd.read_sql(
            text("""
                SELECT r.compteur_id, r.conso_val 
                FROM releves r
                WHERE r.date_releve < :d_start
                AND r.compteur_id IN :ids
                AND r.id = (
                    SELECT r2.id FROM releves r2
                    WHERE r2.compteur_id = r.compteur_id AND r2.date_releve < :d_start
                    ORDER BY r2.date_releve DESC, r2.id DESC
                    LIMIT 1
                )
            """).bindparams(bindparam("ids", expanding=True)),
            conn, params={"d_start": date_d_str, "ids": list(compteur_ids_tuple)}
        )
        dict_prev = dict(zip(df_prev['compteur_id'], df_prev['conso_val'])) if not df_prev.empty else {}

        df_existants = pd.read_sql(
            text("SELECT compteur_id, conso_val FROM releves WHERE semaine_label = :sem AND compteur_id IN :ids").bindparams(bindparam("ids", expanding=True)),
            conn, params={"sem": semaine_label, "ids": list(compteur_ids_tuple)}
        )
        dict_existants = dict(zip(df_existants['compteur_id'], df_existants['conso_val'])) if not df_existants.empty else {}

    return dict_prev, dict_existants

# ==============================================================================
# UTILITAIRES & CONVERSION D'ÉNERGIE
# ==============================================================================
def generate_excel_bytes(df: pd.DataFrame, sheet_name="Données") -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
    return output.getvalue()

def get_all_weeks_of_year(year: int):
    weeks = []
    total_weeks = date(year, 12, 28).isocalendar()[1]
    for w in range(1, total_weeks + 1):
        mon = date.fromisocalendar(year, w, 1)
        sun = date.fromisocalendar(year, w, 7)
        label = f"S{w:02d} ({mon.strftime('%d/%m')} - {sun.strftime('%d/%m/%Y')})"
        weeks.append({"label": label, "week_num": w, "mon": mon, "sun": sun})
    return weeks

MOIS_DEBUT_SAISON = 7
def get_saison_chauffe(d) -> str:
    if d.month >= MOIS_DEBUT_SAISON: return f"{d.year}/{d.year + 1}"
    else: return f"{d.year - 1}/{d.year}"

def convertir_en_mwh_equivalent(valeur: float, unite: str, type_energie: str = "") -> float:
    if valeur is None or pd.isna(valeur): return 0.0
    if type_energie == "Eau froide": return 0.0 # Exclu du cumul énergétique MWh
        
    unite = str(unite).lower().strip()
    if unite in ['mwh']: return float(valeur)
    elif unite in ['m3']: return float(valeur) * 0.01
    elif unite in ['kwh', 'kw']: return float(valeur) / 1000.0
    return float(valeur)

@st.cache_data(ttl=3600, show_spinner="☁️ Récupération de la météo réelle via Open-Meteo...")
def _fetch_dju_hebdo_raw(date_debut_str: str, date_fin_str: str, lat: float, lon: float) -> dict:
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": date_debut_str, "end_date": date_fin_str,
        "daily": ["temperature_2m_max", "temperature_2m_min"],
        "timezone": "Europe/Paris"
    }
    last_error = None
    data = None
    for attempt in range(3):
        try:
            res = requests.get(url, params=params, timeout=8)
            res.raise_for_status()
            data = res.json()
            break
        except Exception as e:
            last_error = e
            if attempt < 2: time.sleep(1.5 * (attempt + 1))
            
    if data is None: raise RuntimeError(f"Échec après 3 tentatives : {last_error}")
    if "daily" not in data or "temperature_2m_max" not in data["daily"]:
        raise RuntimeError("Réponse Open-Meteo inattendue.")
    
    temps_max = data["daily"]["temperature_2m_max"]
    temps_min = data["daily"]["temperature_2m_min"]
    dju_total = sum(max(0.0, 18.0 - (t_min + t_max) / 2) for t_max, t_min in zip(temps_max, temps_min) if t_max is not None and t_min is not None)
    jours_valides = sum(1 for t_max, t_min in zip(temps_max, temps_min) if t_max is not None and t_min is not None)
    
    if jours_valides == 0: raise RuntimeError("Aucune donnée météo disponible pour cette période.")
    return {"dju": round(dju_total, 1), "jours_valides": jours_valides, "jours_total": len(temps_max)}

def fetch_dju_hebdo(date_debut_str: str, date_fin_str: str, lat=45.18, lon=5.73) -> dict:
    try:
        result = _fetch_dju_hebdo_raw(date_debut_str, date_fin_str, lat, lon)
    except Exception as e:
        return {"dju": 100.0, "fiable": False, "message": f"⚠️ Impossible de récupérer la météo réelle ({e}). Valeur par défaut (100.0) appliquée."}
    
    if result["jours_valides"] < result["jours_total"]:
        manquants = result["jours_total"] - result["jours_valides"]
        return {"dju": result["dju"], "fiable": False, "message": f"⚠️ Données météo incomplètes ({manquants} jour(s) manquants sur l'API)."}
    return {"dju": result["dju"], "fiable": True, "message": None}

@st.cache_data(ttl=21600, show_spinner=False)
def refresh_stale_dju():
    limite = (date.today() - timedelta(days=DELAI_DJU_JOURS)).strftime("%Y-%m-%d")
    with engine.connect() as conn:
        semaines_a_verifier = pd.read_sql(text("""
            SELECT semaine_label, MIN(date_releve) as date_fin
            FROM releves WHERE dju_fiable = FALSE AND date_releve <= :limite
            GROUP BY semaine_label LIMIT 3
        """), conn, params={"limite": limite})
    
    nb_corr = 0
    for _, row in semaines_a_verifier.iterrows():
        date_fin = pd.to_datetime(row['date_fin']).date()
        date_debut = date_fin - timedelta(days=6)
        res = fetch_dju_hebdo(date_debut.strftime("%Y-%m-%d"), date_fin.strftime("%Y-%m-%d"))
        if res["fiable"]:
            with engine.begin() as conn:
                rows = conn.execute(text("SELECT id, compteur_id, dju_reels FROM releves WHERE semaine_label = :sem AND dju_fiable = FALSE"), {"sem": row['semaine_label']}).fetchall()
                for r in rows:
                    if abs(float(r[2]) - res["dju"]) > 1e-9:
                        conn.execute(text("""
                            INSERT INTO releves_audit (releve_id, compteur_id, semaine_label, ancienne_valeur, nouvelle_valeur, date_modification, champ_modifie)
                            VALUES (:rid, :cid, :sem, :old, :new, :dt, 'dju')
                        """), {"rid": r[0], "cid": r[1], "sem": row['semaine_label'], "old": float(r[2]), "new": res["dju"], "dt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                conn.execute(text("UPDATE releves SET dju_reels = :dju, dju_fiable = TRUE WHERE semaine_label = :sem AND dju_fiable = FALSE"), {"dju": res["dju"], "sem": row['semaine_label']})
            nb_corr += 1
    return nb_corr

# ==============================================================================
# BARRE LATÉRALE ET NAVIGATION
# ==============================================================================
with st.sidebar:
    st.image("https://img.icons8.com/fluency/96/lightning-bolt.png", width=64)
    st.title("Parc Multi-Compteurs")
    st.caption("Base de données : Supabase")
    
    if st.button("🚪 Déconnexion", use_container_width=True):
        st.session_state["authenticated"] = False
        st.session_state["admin_authenticated"] = False
        st.rerun()
    st.divider()
    menu = st.radio(
        "Navigation", 
        ["📊 Dashboard Global", "📈 Analyse & Courbes par Bâtiment", "🔥 Efficacité MWh/DJU", "📝 Saisie Hebdomadaire", "⚙️ Gestion Sites, Compteurs & Secteurs"],
        index=3
    )

LISTE_SECTEURS = get_secteurs_list()

try:
    nb_dju_corriges = refresh_stale_dju()
    if nb_dju_corriges > 0:
        set_flash(f"🛠️ Le DJU de {nb_dju_corriges} semaine(s) a été mis à jour automatiquement.", "info")
except Exception: pass


# ==============================================================================
# TAB 1: DASHBOARD GLOBAL
# ==============================================================================
if menu == "📊 Dashboard Global":
    st.markdown('<div class="main-header"><h2>📊 Tableau de Bord Multi-Énergies</h2><span>Vue consolidée des bâtiments et sous-compteurs</span></div>', unsafe_allow_html=True)
    display_flash()
    
    with engine.connect() as conn:
        df_semaines = pd.read_sql(text("SELECT DISTINCT semaine_label, date_releve FROM releves ORDER BY date_releve DESC"), conn)
    
    if df_semaines.empty:
        st.info("👋 Aucun relevé enregistré.")
    else:
        semaines_dispo = df_semaines.sort_values('date_releve', ascending=False)['semaine_label'].tolist()
        semaine_sel = st.selectbox("Sélectionner la semaine d'analyse", semaines_dispo)
        
        with engine.connect() as conn:
            df_semaine = pd.read_sql(text("""
                SELECT r.*, c.numero_compteur, c.type_energie, c.unite,
                       s.nom as site_nom, s.secteur, s.surface_m2, s.epoque, s.ordre
                FROM releves r
                JOIN compteurs c ON r.compteur_id = c.id
                JOIN sites s ON c.site_id = s.id
                WHERE r.semaine_label = :sem
            """), conn, params={"sem": semaine_sel})
            
        df_semaine['conso_mwh_eq'] = df_semaine.apply(
            lambda row: convertir_en_mwh_equivalent(row['conso_val'], row['unite'], row['type_energie']), 
            axis=1
        )
        df_bat_semaine = df_semaine.groupby(['site_nom', 'secteur', 'surface_m2', 'epoque', 'ordre']).agg({'conso_mwh_eq': 'sum', 'dju_reels': 'mean'}).reset_index().sort_values(by=['ordre', 'site_nom'])
        df_bat_semaine['ratio_kwh_m2'] = df_bat_semaine.apply(lambda r: (r['conso_mwh_eq'] * 1000) / r['surface_m2'] if r['surface_m2'] > 0 else 0.0, axis=1)
        df_bat_semaine['cible_kwh'] = df_bat_semaine.apply(
            lambda r: (REFERENTIEL_EPOQUES.get(r['epoque'], 200) * (r['dju_reels'] / DJU_ANNUEL_REFERENCE))
            if pd.notna(r['dju_reels']) and r['dju_reels'] > 0 else REFERENTIEL_EPOQUES.get(r['epoque'], 200) * 0.045, axis=1
        )
        df_bat_semaine['ecart_pct'] = df_bat_semaine.apply(lambda r: ((r['ratio_kwh_m2'] - r['cible_kwh']) / r['cible_kwh'] * 100) if r['cible_kwh'] > 0 else 0.0, axis=1)

        k1, k2, k3, k4 = st.columns(4)
        k1.markdown(f'<div class="kpi-card"><div class="kpi-title">Bâtiments Actifs</div><div class="kpi-value">{len(df_bat_semaine)}</div></div>', unsafe_allow_html=True)
        k2.markdown(f'<div class="kpi-card"><div class="kpi-title">Conso Totale Équiv.</div><div class="kpi-value">{df_bat_semaine["conso_mwh_eq"].sum():.1f} <span style="font-size:1rem;">MWh eq</span></div></div>', unsafe_allow_html=True)
        k3.markdown(f'<div class="kpi-card"><div class="kpi-title">Météo Moyenne</div><div class="kpi-value">{df_bat_semaine["dju_reels"].mean():.1f} <span style="font-size:1rem;">DJU</span></div></div>', unsafe_allow_html=True)
        anomalies = df_bat_semaine[df_bat_semaine['ecart_pct'] > 25]
        k4.markdown(f'<div class="kpi-card {"kpi-card-danger" if len(anomalies)>0 else ""}"><div class="kpi-title">Bâtiments en Dérive</div><div class="kpi-value">{len(anomalies)}</div></div>', unsafe_allow_html=True)

        st.divider()
        col_t1, col_t2 = st.columns([3, 1])
        col_t1.subheader("📋 Synthèse par Bâtiment")
        df_export_dash = df_bat_semaine[['site_nom', 'secteur', 'surface_m2', 'conso_mwh_eq', 'ratio_kwh_m2', 'ecart_pct']].rename(
            columns={'site_nom': 'Bâtiment', 'surface_m2': 'Surface (m²)', 'conso_mwh_eq': 'Conso Équiv. (MWh)', 'ratio_kwh_m2': 'kWh/m²', 'ecart_pct': 'Écart Cible (%)'}
        )
        col_t2.download_button(label="📥 Exporter Synthèse Excel", data=generate_excel_bytes(df_export_dash, sheet_name="Synthese_Hebdo"), file_name=f"synthese_{semaine_sel.split(' ')[0]}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.dataframe(df_export_dash, hide_index=True, use_container_width=True)


# ==============================================================================
# TAB 2: ANALYSE PAR BÂTIMENT ET COURBES
# ==============================================================================
elif menu == "📈 Analyse & Courbes par Bâtiment":
    st.markdown('<div class="main-header"><h2>📈 Analyse Détaillée & Courbes par Bâtiment</h2></div>', unsafe_allow_html=True)
    display_flash()
    
    with engine.connect() as conn:
        df_sites = pd.read_sql(text("SELECT * FROM sites ORDER BY ordre ASC, nom ASC"), conn)
    if df_sites.empty:
        st.info("Aucun bâtiment enregistré.")
    else:
        site_dict = {f"{row['nom']} ({row['secteur']})": int(row['id']) for _, row in df_sites.iterrows()}
        selected_label = st.selectbox("Sélectionnez un bâtiment", list(site_dict.keys()))
        site_id = site_dict[selected_label]
        site_info = df_sites[df_sites['id'] == site_id].iloc[0]
        
        with engine.connect() as conn:
            df_compteurs = pd.read_sql(text("SELECT * FROM compteurs WHERE site_id = :s_id"), conn, params={"s_id": site_id})
            df_releves = pd.read_sql(text("""
                SELECT r.semaine_label, r.date_releve, r.conso_val, r.dju_reels, c.numero_compteur, c.type_energie, c.unite
                FROM releves r JOIN compteurs c ON r.compteur_id = c.id
                WHERE c.site_id = :s_id ORDER BY r.date_releve ASC
            """), conn, params={"s_id": site_id})
        
        st.write(f"### 🏢 {site_info['nom']} — Surface : {site_info['surface_m2']} m² ({site_info['epoque']})")
        if df_compteurs.empty:
            st.warning("Aucun sous-compteur associé à ce bâtiment.")
        elif df_releves.empty:
            st.info("Aucun relevé enregistré pour ce bâtiment.")
        else:
            import plotly.graph_objects as go
            from plotly.subplots import make_subplots
            
            df_releves['conso_mwh_eq'] = df_releves.apply(
                lambda r: convertir_en_mwh_equivalent(r['conso_val'], r['unite'], r['type_energie']), 
                axis=1
            )
            semaines_ord = df_releves.sort_values('date_releve')['semaine_label'].unique()
            pivot_compteurs = df_releves.pivot_table(index='semaine_label', columns='numero_compteur', values='conso_mwh_eq', aggfunc='sum').reindex(semaines_ord).fillna(0)
            pivot_compteurs['Consommation Globale (MWh eq)'] = df_releves.groupby('semaine_label')['conso_mwh_eq'].sum().reindex(semaines_ord)
            df_dju = df_releves.groupby('semaine_label')['dju_reels'].mean().to_frame()
            
            col_opt1, col_opt2 = st.columns([3, 1])
            courbes_sel = col_opt1.multiselect("Courbes de consommation :", options=list(pivot_compteurs.columns), default=list(pivot_compteurs.columns))
            afficher_dju = col_opt2.checkbox("Afficher DJU réels", value=True)
            
            if courbes_sel:
                fig = make_subplots(specs=[[{"secondary_y": True}]])
                for col in courbes_sel:
                    fig.add_trace(go.Scatter(x=pivot_compteurs.index, y=pivot_compteurs[col], name=col, mode='lines+markers', hovertemplate="%{y:.2f} MWh<extra></extra>"), secondary_y=False)
                if afficher_dju:
                    fig.add_trace(go.Scatter(x=pivot_compteurs.index, y=df_dju.reindex(pivot_compteurs.index)['dju_reels'], name="DJU Réels", mode='lines', line=dict(color='#f59e0b', width=2.5, dash='dash'), hovertemplate="%{y:.1f} DJU<extra></extra>"), secondary_y=True)
                
                fig.update_layout(title_text=f"Analyse croisée Consommation / DJU — {site_info['nom']}", hovermode="x unified", margin=dict(l=20, r=20, t=50, b=20))
                fig.update_yaxes(title_text="<b>Consommation</b> (MWh eq)", secondary_y=False)
                fig.update_yaxes(title_text="<b>Rigueur Météo</b> (DJU réels)", secondary_y=True)
                st.plotly_chart(fig, use_container_width=True)


# ==============================================================================
# TAB 3: EFFICACITÉ MWh/DJU (CHAUFFAGE GAZ + URBAIN)
# ==============================================================================
elif menu == "🔥 Efficacité MWh/DJU":
    st.markdown('<div class="main-header"><h2>🔥 Efficacité par DJU</h2><span>Chauffage (Gaz + Urbain) ramené à la rigueur climatique</span></div>', unsafe_allow_html=True)
    display_flash()
    
    with engine.connect() as conn:
        df_eff = pd.read_sql(text("""
            SELECT r.semaine_label, r.date_releve, r.conso_val, r.dju_reels, c.unite, c.type_energie
            FROM releves r JOIN compteurs c ON r.compteur_id = c.id
            WHERE c.type_energie IN ('Gaz naturel', 'Chauffage urbain')
        """), conn)
    if df_eff.empty:
        st.info("Aucune donnée de chauffage enregistrée pour le moment.")
    else:
        df_eff['conso_mwh_eq'] = df_eff.apply(lambda r: convertir_en_mwh_equivalent(r['conso_val'], r['unite'], r['type_energie']), axis=1)
        df_eff['date_releve'] = pd.to_datetime(df_eff['date_releve'])
        df_eff['saison'] = df_eff['date_releve'].apply(get_saison_chauffe)
        
        conso_p = df_eff.groupby('saison')['conso_mwh_eq'].sum()
        dju_p = df_eff[['saison', 'semaine_label', 'dju_reels']].drop_duplicates().groupby('saison')['dju_reels'].sum()
        df_ratio = pd.DataFrame({"Consommation (MWh eq)": conso_p, "DJU cumulés": dju_p}).dropna()
        df_ratio["MWh/DJU"] = df_ratio["Consommation (MWh eq)"] / df_ratio["DJU cumulés"]
        df_ratio = df_ratio.sort_index()
        
        st.subheader("📊 Consommation par DJU, saison par saison")
        st.bar_chart(df_ratio["MWh/DJU"])
        st.dataframe(df_ratio.reset_index().rename(columns={"index": "Saison"}), hide_index=True, use_container_width=True)


# ==============================================================================
# TAB 4: SAISIE HEBDOMADAIRE (@st.fragment ISOLÉ AVEC S-1 À DROITE)
# ==============================================================================
elif menu == "📝 Saisie Hebdomadaire":
    st.markdown('<div class="main-header"><h2>📝 Saisie des Index par Compteur</h2><span>Relevé rapide sur le terrain</span></div>', unsafe_allow_html=True)
    display_flash()

    secteur_filtre = st.selectbox("📌 Filtrer par secteur / tournée :", ["Tous les secteurs"] + LISTE_SECTEURS)
    df_compteurs = get_compteurs_par_secteur(secteur_filtre)

    if df_compteurs.empty:
        st.info(f"Aucun sous-compteur trouvé pour le secteur '{secteur_filtre}'.")
    else:
        today_date = date.today()
        all_weeks = get_all_weeks_of_year(today_date.year)
        week_labels = [w["label"] for w in all_weeks]
        default_idx = min(max(0, today_date.isocalendar()[1] - 1), len(all_weeks) - 1)

        col_sem, col_dju = st.columns([3, 1])
        selected_week_label = col_sem.selectbox("🗓️ Choisir la semaine :", options=week_labels, index=default_idx)
        selected_week_data = next(w for w in all_weeks if w["label"] == selected_week_label)
        date_d, date_f = selected_week_data["mon"], selected_week_data["sun"]

        dju_result = fetch_dju_hebdo(date_d.strftime("%Y-%m-%d"), date_f.strftime("%Y-%m-%d"))
        dju_val = col_dju.number_input("DJU Réels (Grenoble)", value=float(dju_result["dju"]))
        if dju_result["message"]: st.warning(dju_result["message"])
        dju_fiable_val = True if abs(dju_val - dju_result["dju"]) > 1e-9 else dju_result["fiable"]

        @st.fragment
        def render_tableau_saisie(df_c, sem_label, dt_d, dt_f, dju_v, dju_f_val):
            c_ids = tuple(df_c['compteur_id'].tolist())
            dict_prev, dict_existants = get_releves_s1_et_actuels(c_ids, dt_d.strftime("%Y-%m-%d"), sem_label)

            df_grid = df_c.copy()
            # 1. Colonne de saisie active
            df_grid['Consommation'] = df_grid['compteur_id'].map(lambda cid: float(dict_existants.get(cid, 0.0)))
            # 2. Colonne S-1 verrouillée placée à droite
            df_grid['Relevé S-1 (Précédent)'] = df_grid['compteur_id'].map(lambda cid: float(dict_prev.get(cid, 0.0)))

            edited_grid = st.data_editor(
                df_grid,
                column_order=[
                    "Bâtiment", "Secteur", "N° Compteur", "Énergie", "Unité", 
                    "Consommation", "Relevé S-1 (Précédent)"
                ],
                column_config={
                    "compteur_id": None, "Ordre": None,
                    "Bâtiment": st.column_config.TextColumn(disabled=True),
                    "Secteur": st.column_config.TextColumn(disabled=True),
                    "N° Compteur": st.column_config.TextColumn(disabled=True),
                    "Énergie": st.column_config.TextColumn(disabled=True),
                    "Unité": st.column_config.TextColumn(disabled=True),
                    "Consommation": st.column_config.NumberColumn("Valeur / Index Conso", min_value=0.0, step=0.1),
                    "Relevé S-1 (Précédent)": st.column_config.NumberColumn("Relevé S-1 (🔒 Verrouillé)", disabled=True, format="%.1f")
                },
                hide_index=True, use_container_width=True, num_rows="fixed",
                key=f"grid_{sem_label}_{hash(c_ids)}"
            )

            c_btn1, c_btn2 = st.columns([2, 1])
            if c_btn1.button("💾 Enregistrer les relevés de cette tournée", type="primary"):
                count, erreurs = 0, []
                with engine.begin() as conn:
                    for _, row in edited_grid.iterrows():
                        val = float(row['Consommation'])
                        if val > 0:
                            c_id = int(row['compteur_id'])
                            d_str = dt_f.strftime("%Y-%m-%d")
                            try:
                                with conn.begin_nested():
                                    existing = conn.execute(text("SELECT id, conso_val FROM releves WHERE compteur_id = :cid AND semaine_label = :sem"), {"cid": c_id, "sem": sem_label}).fetchone()
                                    if existing:
                                        if abs(float(existing[1]) - val) > 1e-9:
                                            conn.execute(text("""
                                                INSERT INTO releves_audit (releve_id, compteur_id, semaine_label, ancienne_valeur, nouvelle_valeur, date_modification, champ_modifie)
                                                VALUES (:rid, :cid, :sem, :old, :new, :dt, 'consommation')
                                            """), {"rid": existing[0], "cid": c_id, "sem": sem_label, "old": float(existing[1]), "new": val, "dt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                                            
                                            conn.execute(text("""
                                                UPDATE releves SET date_releve = :d_str, conso_val = :val, dju_reels = :dju, dju_fiable = :dju_fiable WHERE id = :rid
                                            """), {"d_str": d_str, "val": val, "dju": dju_v, "dju_fiable": dju_f_val, "rid": existing[0]})
                                    else:
                                        conn.execute(text("""
                                            INSERT INTO releves (compteur_id, semaine_label, date_releve, conso_val, dju_reels, dju_fiable)
                                            VALUES (:cid, :sem, :d_str, :val, :dju, :dju_fiable)
                                        """), {"cid": c_id, "sem": sem_label, "d_str": d_str, "val": val, "dju": dju_v, "dju_fiable": dju_f_val})
                                    count += 1
                            except Exception as e:
                                erreurs.append(f"Compteur ID {c_id} : {e}")

                # Invalidation ciblée
                get_releves_s1_et_actuels.clear()
                get_compteurs_par_secteur.clear()

                if erreurs:
                    st.error(f"⚠️ {len(erreurs)} relevé(s) n'ont pas pu être enregistrés :")
                    for err in erreurs: st.error(err)
                if count > 0:
                    set_flash(f"Les relevés de {count} sous-compteur(s) ont été enregistrés avec succès !", "success")
                    st.rerun()

            df_export = edited_grid[['Bâtiment', 'Secteur', 'N° Compteur', 'Énergie', 'Unité', 'Consommation', 'Relevé S-1 (Précédent)']].copy()
            c_btn2.download_button(label="📥 Exporter cette semaine en Excel", data=generate_excel_bytes(df_export, sheet_name=f"Saisie_{sem_label.split(' ')[0]}"), file_name=f"saisie_compteurs_{sem_label.split(' ')[0]}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

        render_tableau_saisie(df_compteurs, selected_week_label, date_d, date_f, dju_val, dju_fiable_val)


# ==============================================================================
# TAB 5: GESTION ET ADMINISTRATION (VERROUILLÉ PAR CODE ADMIN)
# ==============================================================================
elif menu == "⚙️ Gestion Sites, Compteurs & Secteurs":
    st.markdown('<div class="main-header"><h2>⚙️ Administration du Parc</h2><span>Accès restreint aux administrateurs</span></div>', unsafe_allow_html=True)
    display_flash()
    
    # --- VÉRIFICATION DU CODE ADMINISTRATEUR ---
    if "admin_authenticated" not in st.session_state:
        st.session_state["admin_authenticated"] = False

    if not st.session_state["admin_authenticated"]:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.warning("🔒 **Espace Réservé**\n\nCet onglet permet de modifier la structure des bâtiments et des sous-compteurs. Seuls les administrateurs autorisés peuvent y accéder.")
            
            with st.form("form_admin_lock"):
                pwd_admin_input = st.text_input("Code Administrateur :", type="password")
                btn_unlock = st.form_submit_button("🔓 Déverrouiller l'Administration", type="primary", use_container_width=True)
                
                if btn_unlock:
                    expected_admin_pwd = st.secrets.get("ADMIN_PASSWORD", "admin123")
                    if pwd_admin_input == expected_admin_pwd:
                        st.session_state["admin_authenticated"] = True
                        st.success("Accès Administrateur accordé !")
                        st.rerun()
                    else:
                        st.error("🔑 Code Administrateur incorrect.")
        st.stop()  # Bloque le chargement des 8 sous-onglets tant que l'accès admin n'est pas validé

    # --- BANDEAU SUPERIEUR SI ADMIN DÉVERROUILLÉ ---
    col_adm1, col_adm2 = st.columns([3, 1])
    col_adm1.caption("🔓 Vous êtes actuellement connecté en mode **Administrateur**.")
    if col_adm2.button("🔒 Verrouiller l'admin", type="secondary", use_container_width=True):
        st.session_state["admin_authenticated"] = False
        st.rerun()

    # --- LES 8 SOUS-ONGLETS COMPLETS DE GESTION ---
    tab_add_site, tab_edit_site, tab_ordre_sites, tab_add_compteur, tab_edit_compteur, tab_secteurs, tab_list, tab_historique = st.tabs([
        "➕ Ajouter Bâtiment", "✏️ Modifier Site", "🔢 Ordre des Bâtiments",
        "➕ Ajouter Sous-Compteur", "✏️ Modifier Sous-Compteur", "🏷️ Renommer Secteurs", 
        "📋 Liste Globale", "🕓 Historique des Modifications"
    ])
    
    # --------------------------------------------------------------------------
    # 1. AJOUTER BÂTIMENT
    # --------------------------------------------------------------------------
    with tab_add_site:
        st.subheader("➕ Créer un nouveau bâtiment")
        with st.form("form_add_site"):
            c_s1, c_s2 = st.columns(2)
            nom_bat = c_s1.text_input("Nom du Bâtiment")
            secteur_bat = c_s2.selectbox("Secteur", LISTE_SECTEURS)
            surface_bat = c_s1.number_input("Surface chauffée (m²)", min_value=10.0, value=1000.0)
            epoque_bat = c_s2.selectbox("Époque / RT", list(REFERENTIEL_EPOQUES.keys()))
            ordre_bat = c_s1.number_input("Ordre d'affichage (Position)", min_value=0, value=1)
            
            if st.form_submit_button("Enregistrer le bâtiment", type="primary"):
                if not nom_bat.strip():
                    st.error("🚨 Le nom du bâtiment ne peut pas être vide.")
                else:
                    try:
                        with engine.begin() as conn:
                            conn.execute(text("""
                                INSERT INTO sites (nom, secteur, surface_m2, epoque, ordre)
                                VALUES (:nom, :sec, :surf, :epoque, :ordre)
                            """), {"nom": nom_bat.strip(), "sec": secteur_bat, "surf": float(surface_bat), "epoque": epoque_bat, "ordre": int(ordre_bat)})
                        
                        get_compteurs_par_secteur.clear()
                        set_flash(f"Le bâtiment '{nom_bat.strip()}' a été créé avec succès !", "success")
                        st.rerun()
                    except Exception:
                        st.error("🚨 Un bâtiment portant ce nom existe déjà dans la base.")

    # --------------------------------------------------------------------------
    # 2. MODIFIER SITE
    # --------------------------------------------------------------------------
    with tab_edit_site:
        st.subheader("✏️ Modifier les caractéristiques, renommer ou supprimer un site")
        with engine.connect() as conn:
            sites_db = pd.read_sql(text("SELECT id, nom, secteur, surface_m2, epoque, ordre FROM sites ORDER BY ordre ASC, nom ASC"), conn).to_dict('records')
        if not sites_db:
            st.info("Aucun bâtiment à modifier.")
        else:
            site_dict_edit = {f"{row['nom']} (Ordre: {row['ordre']})": int(row['id']) for row in sites_db}
            choix_site_edit = st.selectbox("Sélectionnez le bâtiment à modifier", list(site_dict_edit.keys()), key="select_edit_site_box")
            site_id_selected = site_dict_edit[choix_site_edit]
            
            with engine.connect() as conn:
                site_actuel = pd.read_sql(text("SELECT * FROM sites WHERE id = :sid"), conn, params={"sid": site_id_selected}).iloc[0]
                
            if site_actuel is not None:
                with st.form(key=f"form_edit_site_{site_id_selected}"):
                    nouveau_nom = st.text_input("Nouveau nom du Bâtiment", value=site_actuel['nom'])
                    nouveau_secteur = st.selectbox("Secteur", LISTE_SECTEURS, index=LISTE_SECTEURS.index(site_actuel['secteur']) if site_actuel['secteur'] in LISTE_SECTEURS else 0)
                    nouvelle_surface = st.number_input("Surface chauffée (m²)", min_value=10.0, value=float(site_actuel['surface_m2']))
                    nouvelle_epoque = st.selectbox("Époque / RT", list(REFERENTIEL_EPOQUES.keys()), index=list(REFERENTIEL_EPOQUES.keys()).index(site_actuel['epoque']) if site_actuel['epoque'] in REFERENTIEL_EPOQUES else 0)
                    nouvel_ordre = st.number_input("Ordre d'affichage", min_value=0, value=int(site_actuel['ordre'] if pd.notna(site_actuel['ordre']) else 0))
                    
                    if st.form_submit_button("💾 Enregistrer les modifications", type="primary"):
                        if not nouveau_nom.strip():
                            st.error("🚨 Le nom du bâtiment ne peut pas être vide.")
                        else:
                            try:
                                with engine.begin() as conn:
                                    conn.execute(text("""
                                        UPDATE sites 
                                        SET nom = :nom, secteur = :sec, surface_m2 = :surf, epoque = :epoque, ordre = :ordre
                                        WHERE id = :sid
                                    """), {"nom": nouveau_nom.strip(), "sec": nouveau_secteur, "surf": float(nouvelle_surface), "epoque": nouvelle_epoque, "ordre": int(nouvel_ordre), "sid": site_id_selected})
                                
                                get_compteurs_par_secteur.clear()
                                set_flash(f"Les modifications du bâtiment '{nouveau_nom.strip()}' ont été enregistrées !", "success")
                                st.rerun()
                            except Exception as e:
                                st.error(f"🚨 Impossible de mettre à jour le bâtiment : {e}")
                                
                st.divider()
                if st.button(f"🗑️ Supprimer définitivement le bâtiment '{site_actuel['nom']}'", key=f"btn_del_site_{site_id_selected}", type="secondary"):
                    with engine.begin() as conn:
                        conn.execute(text("DELETE FROM sites WHERE id = :sid"), {"sid": site_id_selected})
                    
                    get_compteurs_par_secteur.clear()
                    get_releves_s1_et_actuels.clear()
                    set_flash(f"Le bâtiment '{site_actuel['nom']}' et ses sous-compteurs ont été supprimés !", "info")
                    st.rerun()

    # --------------------------------------------------------------------------
    # 3. ORDRE DES BÂTIMENTS
    # --------------------------------------------------------------------------
    with tab_ordre_sites:
        st.subheader("🔢 Organiser l'ordre d'affichage des Bâtiments")
        with engine.connect() as conn:
            df_ordre_sites = pd.read_sql(text("""SELECT id, nom as "Bâtiment", secteur as "Secteur", ordre as "Ordre" FROM sites ORDER BY ordre ASC, nom ASC"""), conn)
        if df_ordre_sites.empty:
            st.info("Aucun bâtiment dans la base.")
        else:
            edited_ordre_grid = st.data_editor(
                df_ordre_sites,
                column_config={
                    "id": None,
                    "Bâtiment": st.column_config.TextColumn(disabled=True),
                    "Secteur": st.column_config.TextColumn(disabled=True),
                    "Ordre": st.column_config.NumberColumn("Ordre d'affichage", min_value=0, step=1)
                },
                hide_index=True, use_container_width=True, key="grid_reordre_sites"
            )
            if st.button("💾 Enregistrer le nouvel ordre des bâtiments", type="primary"):
                with engine.begin() as conn:
                    for _, row in edited_ordre_grid.iterrows():
                        conn.execute(text("UPDATE sites SET ordre = :o WHERE id = :sid"), {"o": int(row['Ordre']), "sid": int(row['id'])})
                
                get_compteurs_par_secteur.clear()
                set_flash("L'ordre d'affichage des bâtiments a été mis à jour !", "success")
                st.rerun()

    # --------------------------------------------------------------------------
    # 4. AJOUTER SOUS-COMPTEUR
    # --------------------------------------------------------------------------
    with tab_add_compteur:
        st.subheader("➕ Rattacher un sous-compteur à un bâtiment")
        with engine.connect() as conn:
            sites_for_compteurs = pd.read_sql(text("SELECT id, nom, secteur FROM sites ORDER BY ordre ASC, nom ASC"), conn).to_dict('records')
        if not sites_for_compteurs:
            st.info("Aucun bâtiment disponible. Créez d'abord un bâtiment.")
        else:
            secteur_filtre_compteur = st.selectbox("📌 Filtrer les bâtiments par secteur :", ["Tous les secteurs"] + LISTE_SECTEURS, key="filter_sec_add_subcompteur")
            sites_filtered = [s for s in sites_for_compteurs if s['secteur'] == secteur_filtre_compteur] if secteur_filtre_compteur != "Tous les secteurs" else sites_for_compteurs
            if not sites_filtered:
                st.info(f"Aucun bâtiment trouvé dans le secteur '{secteur_filtre_compteur}'.")
            else:
                site_dict_add = {f"{s['nom']} ({s['secteur']})": int(s['id']) for s in sites_filtered}
                with st.form("form_add_subcompteur"):
                    sel_site_label = st.selectbox("Bâtiment concerné", list(site_dict_add.keys()))
                    num_c = st.text_input("Numéro du sous-compteur (ex: EF-SUB-01, CLIM-EG-01)")
                    
                    type_e = st.selectbox("Type d'énergie / Fluide", LISTE_TYPES_ENERGIE)
                    unite_c = st.selectbox("Unité de mesure", ["m3", "kWh", "MWh"])
                    st.caption("ℹ️ 'm³' convient pour le gaz, l'eau froide et l'ECS. Pour l'électricité, le chauffage urbain et l'eau glacée, utilisez kWh ou MWh.")
                    
                    if st.form_submit_button("Ajouter le sous-compteur", type="primary"):
                        if not num_c.strip():
                            st.error("🚨 Le numéro de sous-compteur ne peut pas être vide.")
                        elif unite_c not in UNITES_PAR_ENERGIE.get(type_e, []):
                            st.error(f"🚨 L'unité '{unite_c}' n'est pas cohérente avec '{type_e}'. Unités valides : {', '.join(UNITES_PAR_ENERGIE.get(type_e, []))}.")
                        else:
                            try:
                                with engine.begin() as conn:
                                    conn.execute(text("""
                                        INSERT INTO compteurs (site_id, numero_compteur, type_energie, unite)
                                        VALUES (:sid, :num, :type_e, :unite)
                                    """), {"sid": site_dict_add[sel_site_label], "num": num_c.strip(), "type_e": type_e, "unite": unite_c})
                                
                                get_compteurs_par_secteur.clear()
                                set_flash(f"Le sous-compteur '{num_c.strip()}' ({type_e}) a été ajouté avec succès !", "success")
                                st.rerun()
                            except Exception:
                                st.error("🚨 Ce numéro de sous-compteur existe déjà dans la base.")

    # --------------------------------------------------------------------------
    # 5. MODIFIER / REATTRIBUER / SUPPRIMER SOUS-COMPTEUR
    # --------------------------------------------------------------------------
    with tab_edit_compteur:
        st.subheader("✏️ Modifier, Réattribuer ou Supprimer un Sous-Compteur")
        with engine.connect() as conn:
            compteurs_db = pd.read_sql(text("""
                SELECT c.id, c.site_id, c.numero_compteur, c.type_energie, c.unite, s.nom as site_nom, s.secteur 
                FROM compteurs c JOIN sites s ON c.site_id = s.id 
                ORDER BY s.ordre ASC, s.nom ASC, c.numero_compteur ASC
            """), conn).to_dict('records')
            all_sites_db = pd.read_sql(text("SELECT id, nom, secteur FROM sites ORDER BY ordre ASC, nom ASC"), conn).to_dict('records')
            
        if not compteurs_db:
            st.info("Aucun sous-compteur enregistré.")
        else:
            compteur_dict_edit = {f"{r['site_nom']} ➔ {r['numero_compteur']} ({r['type_energie']})": int(r['id']) for r in compteurs_db}
            choix_c_edit = st.selectbox("Sélectionnez le sous-compteur à modifier", list(compteur_dict_edit.keys()), key="select_edit_compteur_box")
            compteur_id_selected = compteur_dict_edit[choix_c_edit]
            
            with engine.connect() as conn:
                c_actuel = pd.read_sql(text("SELECT * FROM compteurs WHERE id = :cid"), conn, params={"cid": compteur_id_selected}).iloc[0]
                
            if c_actuel is not None:
                site_options = {f"{s['nom']} ({s['secteur']})": int(s['id']) for s in all_sites_db}
                current_site_id = int(c_actuel['site_id'])
                current_site_label = [k for k, v in site_options.items() if v == current_site_id]
                default_site_idx = list(site_options.keys()).index(current_site_label[0]) if current_site_label else 0
                
                with st.form(key=f"form_edit_compteur_{compteur_id_selected}"):
                    nouveau_site_label = st.selectbox("🏢 Bâtiment rattaché (Réattribution)", list(site_options.keys()), index=default_site_idx)
                    nouveau_site_id = site_options[nouveau_site_label]
                    nouveau_num = st.text_input("Numéro du sous-compteur", value=c_actuel['numero_compteur'])
                    
                    energies_possibles = LISTE_TYPES_ENERGIE
                    nouveau_type = st.selectbox("Type d'énergie / Fluide", energies_possibles, index=energies_possibles.index(c_actuel['type_energie']) if c_actuel['type_energie'] in energies_possibles else 0)
                    
                    unites_possibles = ["m3", "MWh", "kWh"]
                    nouvelle_unite = st.selectbox("Unité de mesure", unites_possibles, index=unites_possibles.index(c_actuel['unite']) if c_actuel['unite'] in unites_possibles else 0)
                    
                    if st.form_submit_button("💾 Enregistrer les modifications", type="primary"):
                        if not nouveau_num.strip():
                            st.error("🚨 Le numéro de sous-compteur ne peut pas être vide.")
                        elif nouvelle_unite not in UNITES_PAR_ENERGIE.get(nouveau_type, []):
                            st.error(f"🚨 L'unité '{nouvelle_unite}' n'est pas cohérente avec '{nouveau_type}'. Unités valides : {', '.join(UNITES_PAR_ENERGIE.get(nouveau_type, []))}.")
                        else:
                            try:
                                with engine.begin() as conn:
                                    conn.execute(text("""
                                        UPDATE compteurs 
                                        SET site_id = :sid, numero_compteur = :num, type_energie = :te, unite = :unite
                                        WHERE id = :cid
                                    """), {"sid": nouveau_site_id, "num": nouveau_num.strip(), "te": nouveau_type, "unite": nouvelle_unite, "cid": compteur_id_selected})
                                
                                get_compteurs_par_secteur.clear()
                                set_flash(f"Le sous-compteur '{nouveau_num.strip()}' a été mis à jour !", "success")
                                st.rerun()
                            except Exception:
                                st.error("🚨 Un autre sous-compteur porte déjà ce numéro dans la base.")
                                
                st.divider()
                if st.button("🗑️ Supprimer définitivement ce sous-compteur", key=f"btn_del_compteur_{compteur_id_selected}", type="secondary"):
                    with engine.begin() as conn:
                        conn.execute(text("DELETE FROM compteurs WHERE id = :cid"), {"cid": compteur_id_selected})
                    
                    get_compteurs_par_secteur.clear()
                    get_releves_s1_et_actuels.clear()
                    set_flash("Le sous-compteur a été supprimé de la base !", "info")
                    st.rerun()

    # --------------------------------------------------------------------------
    # 6. RENOMMER LES SECTEURS
    # --------------------------------------------------------------------------
    with tab_secteurs:
        st.subheader("🏷️ Personnaliser et renommer les secteurs")
        with engine.connect() as conn:
            secteurs_rows = pd.read_sql(text("SELECT id, nom FROM secteurs ORDER BY id"), conn).to_dict('records')
            
        with st.form("form_renommer_secteurs"):
            nouveaux_noms_map = {}
            for row in secteurs_rows:
                sec_id = row['id']
                old_name = row['nom']
                nouveau = st.text_input(f"Nom du secteur (ID {sec_id})", value=old_name, key=f"sec_input_{sec_id}")
                nouveaux_noms_map[sec_id] = (old_name, nouveau.strip())
                
            if st.form_submit_button("💾 Sauvegarder les nouveaux noms de secteurs", type="primary"):
                all_new_names = [v[1] for v in nouveaux_noms_map.values()]
                if any(len(name) == 0 for name in all_new_names):
                    st.error("🚨 Aucun nom de secteur ne peut être vide.")
                elif len(all_new_names) != len(set(all_new_names)):
                    st.error("🚨 Tous les noms de secteurs doivent être uniques.")
                else:
                    with engine.begin() as conn:
                        for sec_id, (old_name, new_name) in nouveaux_noms_map.items():
                            if old_name != new_name:
                                conn.execute(text("UPDATE secteurs SET nom = :n WHERE id = :sid"), {"n": new_name, "sid": sec_id})
                                conn.execute(text("UPDATE sites SET secteur = :n WHERE secteur = :old"), {"n": new_name, "old": old_name})
                    
                    get_secteurs_list.clear()
                    get_compteurs_par_secteur.clear()
                    set_flash("Tous les secteurs ont été renommés avec succès !", "success")
                    st.rerun()

    # --------------------------------------------------------------------------
    # 7. LISTE GLOBALE DU PARC
    # --------------------------------------------------------------------------
    with tab_list:
        with engine.connect() as conn:
            df_all = pd.read_sql(text("""
                SELECT s.ordre as "Ordre", s.nom as "Bâtiment", s.secteur as "Secteur", s.surface_m2 as "Surface", s.epoque as "Époque RT",
                       c.numero_compteur as "N° Compteur", c.type_energie as "Énergie", c.unite as "Unité"
                FROM sites s LEFT JOIN compteurs c ON s.id = c.site_id
                ORDER BY s.ordre ASC, s.nom ASC
            """), conn)
        col_l1, col_l2 = st.columns([3, 1])
        col_l1.subheader("📋 Répertoire complet du parc municipal")
        col_l2.download_button(label="📥 Exporter le parc en Excel", data=generate_excel_bytes(df_all, sheet_name="Parc_Complet"), file_name="parc_batiments_compteurs.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
        st.dataframe(df_all, hide_index=True, use_container_width=True)

    # --------------------------------------------------------------------------
    # 8. HISTORIQUE DES MODIFICATIONS (AUDIT TRAIL)
    # --------------------------------------------------------------------------
    with tab_historique:
        st.subheader("🕓 Historique des corrections de relevés")
        st.caption("Traçabilité des modifications effectuées sur les index déjà enregistrés.")
        with engine.connect() as conn:
            df_audit = pd.read_sql(text("""
                SELECT a.date_modification as "Date modification", s.nom as "Bâtiment", c.numero_compteur as "N° Compteur",
                       a.semaine_label as "Semaine", a.champ_modifie as "Champ modifié", a.ancienne_valeur as "Ancienne valeur", a.nouvelle_valeur as "Nouvelle valeur"
                FROM releves_audit a
                JOIN compteurs c ON a.compteur_id = c.id
                JOIN sites s ON c.site_id = s.id
                ORDER BY a.id DESC
                LIMIT 100
            """), conn)
        if df_audit.empty:
            st.info("Aucune correction de relevé enregistrée pour le moment.")
        else:
            st.dataframe(df_audit, hide_index=True, use_container_width=True)
