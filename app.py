import streamlit as st
import pandas as pd
import requests
import contextlib
import io
import os
from datetime import datetime, timedelta, date
from sqlalchemy import create_engine, text

# --- CONFIGURATION PAGE STREAMLIT ---
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
        color: white; padding: 18px 24px; border-radius: 12px; margin-bottom: 24px;
    }
</style>
""", unsafe_allow_html=True)

# --- AUTHENTIFICATION ---
MOT_DE_PASSE_PAR_DEFAUT = "Mairie2026"

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

def check_password():
    if not st.session_state["authenticated"]:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.markdown('<div class="main-header" style="text-align: center;"><h2>🔒 Accès Sécurisé</h2><span>Suivi Énergétique du Parc Municipal</span></div>', unsafe_allow_html=True)
            with st.form("form_login"):
                pwd_input = st.text_input("Veuillez saisir le mot de passe :", type="password")
                submit_login = st.form_submit_button("Se connecter", type="primary", use_container_width=True)
                
                if submit_login:
                    expected_pwd = st.secrets.get("APP_PASSWORD", MOT_DE_PASSE_PAR_DEFAUT)
                    if pwd_input == expected_pwd:
                        st.session_state["authenticated"] = True
                        st.rerun()
                    else:
                        st.error("🔑 Mot de passe incorrect.")
        return False
    return True

if not check_password():
    st.stop()

# --- NOTIFICATIONS ---
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

# --- CONNEXION BASE DE DONNÉES (SUPABASE OU SQLITE FALLBACK) ---
@st.cache_resource
def get_db_engine():
    if "db" in st.secrets and "url" in st.secrets["db"]:
        db_url = st.secrets["db"]["url"]
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return create_engine(db_url, pool_pre_ping=True)
    else:
        # Repli local SQLite si pas de configuration Supabase
        return create_engine("sqlite:///parc_energie_multicompteurs.db")

engine = get_db_engine()

def init_db():
    is_postgres = "postgresql" in str(engine.url)
    pk_auto = "SERIAL PRIMARY KEY" if is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS secteurs (
                id {pk_auto},
                nom VARCHAR(255) UNIQUE NOT NULL
            );
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS sites (
                id {pk_auto},
                nom VARCHAR(255) UNIQUE NOT NULL,
                secteur VARCHAR(255) NOT NULL,
                surface_m2 FLOAT NOT NULL,
                epoque VARCHAR(255) NOT NULL,
                ordre INT DEFAULT 0
            );
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS compteurs (
                id {pk_auto},
                site_id INT NOT NULL,
                numero_compteur VARCHAR(255) UNIQUE NOT NULL,
                type_energie VARCHAR(255) NOT NULL,
                unite VARCHAR(255) NOT NULL,
                FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
            );
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS releves (
                id {pk_auto},
                compteur_id INT NOT NULL,
                semaine_label VARCHAR(255) NOT NULL,
                date_releve DATE NOT NULL,
                conso_val FLOAT NOT NULL,
                dju_reels FLOAT NOT NULL,
                FOREIGN KEY (compteur_id) REFERENCES compteurs(id) ON DELETE CASCADE
            );
        """))
        
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_releves_compteur_semaine ON releves(compteur_id, semaine_label);"))
        except Exception:
            pass

        res = conn.execute(text("SELECT COUNT(*) FROM secteurs")).scalar()
        if res == 0:
            for s in DEFAULT_SECTEURS:
                conn.execute(text("INSERT INTO secteurs (nom) VALUES (:nom) ON CONFLICT DO NOTHING;"), {"nom": s})

init_db()

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

def get_secteurs_list():
    with engine.connect() as conn:
        df = pd.read_sql("SELECT nom FROM secteurs ORDER BY id", conn)
    return df['nom'].tolist() if not df.empty else DEFAULT_SECTEURS

def convertir_en_mwh_equivalent(valeur: float, unite: str) -> float:
    if valeur is None or pd.isna(valeur):
        return 0.0
    unite = str(unite).lower().strip()
    if unite in ['mwh']:
        return float(valeur)
    elif unite in ['m3']:
        return float(valeur) * 0.01
    elif unite in ['kwh', 'kw']:
        return float(valeur) / 1000.0
    return float(valeur)

@st.cache_data(ttl=3600)
def fetch_dju_hebdo(date_debut_str: str, date_fin_str: str, lat=45.18, lon=5.73) -> float:
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": date_debut_str, "end_date": date_fin_str,
        "daily": ["temperature_2m_max", "temperature_2m_min"],
        "timezone": "Europe/Paris"
    }
    try:
        res = requests.get(url, params=params, timeout=5)
        res.raise_for_status()
        data = res.json()
        dju_total = 0.0
        if "daily" in data and "temperature_2m_max" in data["daily"]:
            for i in range(len(data["daily"]["time"])):
                t_max = data["daily"]["temperature_2m_max"][i]
                t_min = data["daily"]["temperature_2m_min"][i]
                if t_max is not None and t_min is not None:
                    t_moy = (t_min + t_max) / 2
                    dju_total += max(0.0, 18.0 - t_moy)
            return round(dju_total, 1)
        return 100.0
    except Exception:
        return 100.0

# --- NAVIGATION ---
with st.sidebar:
    st.image("https://img.icons8.com/fluency/96/lightning-bolt.png", width=64)
    st.title("Parc Multi-Compteurs")
    st.caption("Base de données en ligne : Supabase")
    
    if st.button("🚪 Déconnexion", use_container_width=True):
        st.session_state["authenticated"] = False
        st.rerun()

    st.divider()
    menu = st.radio(
        "Navigation", 
        ["📊 Dashboard Global", "📈 Analyse & Courbes par Bâtiment", "📝 Saisie Hebdomadaire", "⚙️ Gestion Sites, Compteurs & Secteurs"],
        index=0
    )

LISTE_SECTEURS = get_secteurs_list()

# ==============================================================================
# TAB 1: DASHBOARD GLOBAL
# ==============================================================================
if menu == "📊 Dashboard Global":
    st.markdown('<div class="main-header"><h2>📊 Tableau de Bord Multi-Énergies</h2><span>Vue consolidée des bâtiments et de leurs sous-compteurs</span></div>', unsafe_allow_html=True)
    display_flash()
    
    with engine.connect() as conn:
        df_releves = pd.read_sql("""
            SELECT r.*, c.numero_compteur, c.type_energie, c.unite, 
                   s.nom as site_nom, s.secteur, s.surface_m2, s.epoque, s.ordre
            FROM releves r 
            JOIN compteurs c ON r.compteur_id = c.id
            JOIN sites s ON c.site_id = s.id
            ORDER BY r.date_releve DESC
        """, conn)

    if df_releves.empty:
        st.info("👋 Aucun relevé enregistré. Rendez-vous dans l'onglet **Gestion Sites, Compteurs & Secteurs** pour ajouter vos bâtiments.")
    else:
        semaines_dispo = df_releves[['semaine_label', 'date_releve']].drop_duplicates().sort_values('date_releve', ascending=False)['semaine_label'].tolist()
        semaine_sel = st.selectbox("Sélectionner la semaine d'analyse", semaines_dispo)

        df_semaine = df_releves[df_releves['semaine_label'] == semaine_sel].copy()
        df_semaine['conso_mwh_eq'] = df_semaine.apply(lambda row: convertir_en_mwh_equivalent(row['conso_val'], row['unite']), axis=1)
        
        df_bat_semaine = df_semaine.groupby(['site_nom', 'secteur', 'surface_m2', 'epoque', 'ordre']).agg({
            'conso_mwh_eq': 'sum',
            'dju_reels': 'mean'
        }).reset_index().sort_values(by=['ordre', 'site_nom'])

        df_bat_semaine['ratio_kwh_m2'] = df_bat_semaine.apply(
            lambda r: (r['conso_mwh_eq'] * 1000) / r['surface_m2'] if r['surface_m2'] > 0 else 0.0, axis=1
        )
        df_bat_semaine['cible_kwh'] = df_bat_semaine['epoque'].map(REFERENTIEL_EPOQUES).fillna(200) * 0.045
        df_bat_semaine['ecart_pct'] = df_bat_semaine.apply(
            lambda r: ((r['ratio_kwh_m2'] - r['cible_kwh']) / r['cible_kwh'] * 100) if r['cible_kwh'] > 0 else 0.0, axis=1
        )

        k1, k2, k3, k4 = st.columns(4)
        k1.markdown(f'<div class="kpi-card"><div class="kpi-title">Bâtiments Actifs</div><div class="kpi-value">{len(df_bat_semaine)}</div></div>', unsafe_allow_html=True)
        k2.markdown(f'<div class="kpi-card"><div class="kpi-title">Conso Totale Équivalente</div><div class="kpi-value">{df_bat_semaine["conso_mwh_eq"].sum():.1f} <span style="font-size:1rem;">MWh eq</span></div></div>', unsafe_allow_html=True)
        k3.markdown(f'<div class="kpi-card"><div class="kpi-title">Météo Moyenne</div><div class="kpi-value">{df_bat_semaine["dju_reels"].mean():.1f} <span style="font-size:1rem;">DJU</span></div></div>', unsafe_allow_html=True)
        anomalies = df_bat_semaine[df_bat_semaine['ecart_pct'] > 25]
        k4.markdown(f'<div class="kpi-card {"kpi-card-danger" if len(anomalies)>0 else ""}"><div class="kpi-title">Bâtiments en Dérive</div><div class="kpi-value">{len(anomalies)}</div></div>', unsafe_allow_html=True)

        st.divider()
        col_t1, col_t2 = st.columns([3, 1])
        col_t1.subheader("📋 Synthèse par Bâtiment (Cumul de tous les sous-compteurs)")
        
        df_export_dash = df_bat_semaine[['site_nom', 'secteur', 'surface_m2', 'conso_mwh_eq', 'ratio_kwh_m2', 'ecart_pct']].rename(columns={
            'site_nom': 'Bâtiment', 'surface_m2': 'Surface (m²)', 'conso_mwh_eq': 'Conso Équiv. (MWh)', 'ratio_kwh_m2': 'kWh/m²', 'ecart_pct': 'Écart Cible (%)'
        })
        
        col_t2.download_button(
            label="📥 Exporter cette synthèse en Excel",
            data=generate_excel_bytes(df_export_dash, sheet_name="Synthese_Hebdo"),
            file_name=f"synthese_{semaine_sel.split(' ')[0]}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        st.dataframe(df_export_dash, hide_index=True, use_container_width=True)

# ==============================================================================
# TAB 2: ANALYSE & COURBES PAR BÂTIMENT
# ==============================================================================
elif menu == "📈 Analyse & Courbes par Bâtiment":
    st.markdown('<div class="main-header"><h2>📈 Analyse Détaillée & Courbes par Bâtiment</h2><span>Courbe globale et courbes individuelles des sous-compteurs</span></div>', unsafe_allow_html=True)
    display_flash()
    
    with engine.connect() as conn:
        df_sites = pd.read_sql("SELECT * FROM sites ORDER BY ordre ASC, nom ASC", conn)

    if df_sites.empty:
        st.info("Aucun site enregistré.")
    else:
        site_dict = {f"{row['nom']} ({row['secteur']})": int(row['id']) for _, row in df_sites.iterrows()}
        selected_label = st.selectbox("Sélectionnez un bâtiment", list(site_dict.keys()))
        site_id = site_dict[selected_label]
        
        site_info = df_sites[df_sites['id'] == site_id].iloc[0]

        with engine.connect() as conn:
            df_compteurs = pd.read_sql("SELECT * FROM compteurs WHERE site_id = %(s_id)s", conn, params={"s_id": site_id})
            df_releves = pd.read_sql("""
                SELECT r.semaine_label, r.date_releve, r.conso_val, r.dju_reels, c.numero_compteur, c.type_energie, c.unite 
                FROM releves r JOIN compteurs c ON r.compteur_id = c.id 
                WHERE c.site_id = %(s_id)s
                ORDER BY r.date_releve ASC
            """, conn, params={"s_id": site_id})

        st.write(f"### 🏢 {site_info['nom']} — Surface : {site_info['surface_m2']} m² ({site_info['epoque']})")
        
        if df_compteurs.empty:
            st.warning("Aucun compteur associé à ce bâtiment.")
        else:
            st.write("#### 🔌 Sous-compteurs installés :")
            st.dataframe(df_compteurs[['numero_compteur', 'type_energie', 'unite']], hide_index=True, use_container_width=True)

            if df_releves.empty:
                st.info("Aucun relevé enregistré pour ce bâtiment.")
            else:
                df_releves['conso_mwh_eq'] = df_releves.apply(lambda r: convertir_en_mwh_equivalent(r['conso_val'], r['unite']), axis=1)
                semaines_ordonnees = df_releves.sort_values('date_releve')['semaine_label'].unique()

                pivot_compteurs = df_releves.pivot_table(index='semaine_label', columns='numero_compteur', values='conso_val', aggfunc='sum').reindex(semaines_ordonnees).fillna(0)
                global_conso = df_releves.groupby('semaine_label')['conso_mwh_eq'].sum().reindex(semaines_ordonnees).rename("⚡ Consommation Globale (MWh eq)")
                
                df_courbes = pivot_compteurs.copy()
                df_courbes['Consommation Globale (MWh eq)'] = global_conso

                st.subheader("📉 Options d'affichage des courbes")
                toutes_courbes = list(df_courbes.columns)
                courbes_selectionnees = st.multiselect("Choisissez les courbes à faire apparaître sur le graphique :", options=toutes_courbes, default=toutes_courbes)

                if courbes_selectionnees:
                    st.line_chart(df_courbes[courbes_selectionnees])
                else:
                    st.warning("Veuillez sélectionner au moins une courbe à afficher.")

                st.subheader("📋 Historique complet des relevés")
                st.dataframe(df_releves[['semaine_label', 'date_releve', 'numero_compteur', 'type_energie', 'conso_val', 'unite', 'dju_reels']], hide_index=True, use_container_width=True)

# ==============================================================================
# TAB 3: SAISIE HEBDOMADAIRE
# ==============================================================================
elif menu == "📝 Saisie Hebdomadaire":
    st.markdown('<div class="main-header"><h2>📝 Saisie des Index par Compteur</h2><span>Relevé hebdomadaire multi-énergies par tournée / secteur</span></div>', unsafe_allow_html=True)
    display_flash()
    
    with engine.connect() as conn:
        df_compteurs = pd.read_sql("""
            SELECT c.id as compteur_id, s.nom as Bâtiment, s.secteur as Secteur, s.ordre as Ordre,
                   c.numero_compteur as 'N° Compteur', c.type_energie as Énergie, c.unite as Unité
            FROM compteurs c JOIN sites s ON c.site_id = s.id
            ORDER BY s.ordre ASC, s.nom ASC, c.numero_compteur ASC
        """, conn)

    if df_compteurs.empty:
        st.warning("Aucun compteur enregistré dans la base.")
    else:
        secteur_filtre = st.selectbox("📌 Filtrer par secteur / tournée :", ["Tous les secteurs"] + LISTE_SECTEURS)
        if secteur_filtre != "Tous les secteurs":
            df_compteurs = df_compteurs[df_compteurs['Secteur'] == secteur_filtre]

        if df_compteurs.empty:
            st.info(f"Aucun sous-compteur trouvé pour le secteur '{secteur_filtre}'.")
        else:
            today_date = date.today()
            current_year = today_date.year
            current_week_num = today_date.isocalendar()[1]
            
            all_weeks = get_all_weeks_of_year(current_year)
            week_labels = [w["label"] for w in all_weeks]
            default_idx = min(max(0, current_week_num - 1), len(all_weeks) - 1)

            col_sem, col_dju = st.columns([3, 1])
            selected_week_label = col_sem.selectbox("🗓️ Choisir la semaine dans toute l'année (Pré-sélection : Semaine en cours) :", options=week_labels, index=default_idx)

            selected_week_data = next(w for w in all_weeks if w["label"] == selected_week_label)
            date_d = selected_week_data["mon"]
            date_f = selected_week_data["sun"]
            semaine_label = selected_week_data["label"]

            dju_auto = fetch_dju_hebdo(date_d.strftime("%Y-%m-%d"), date_f.strftime("%Y-%m-%d"))
            dju_val = col_dju.number_input("DJU Réels (Grenoble)", value=float(dju_auto))

            with engine.connect() as conn:
                df_prev = pd.read_sql("""
                    SELECT r.compteur_id, r.conso_val 
                    FROM releves r
                    WHERE r.date_releve < %(d_start)s 
                    AND r.id IN (
                        SELECT MAX(id) FROM releves WHERE date_releve < %(d_start)s GROUP BY compteur_id
                    )
                """, conn, params={"d_start": date_d.strftime("%Y-%m-%d")})
                
                dict_prev = dict(zip(df_prev['compteur_id'], df_prev['conso_val'])) if not df_prev.empty else {}

                df_existants = pd.read_sql(
                    "SELECT compteur_id, conso_val FROM releves WHERE semaine_label = %(sem)s",
                    conn, params={"sem": semaine_label}
                )

            df_grid = df_compteurs.copy()
            df_grid['Relevé S-1 (Précédent)'] = df_grid['compteur_id'].map(lambda cid: float(dict_prev.get(cid, 0.0)))

            if not df_existants.empty:
                dict_existants = dict(zip(df_existants['compteur_id'], df_existants['conso_val']))
                df_grid['Consommation'] = df_grid['compteur_id'].map(lambda cid: float(dict_existants.get(cid, 0.0)))
                nb_saisis = len([v for v in dict_existants.values() if float(v) > 0])
                if nb_saisis > 0:
                    st.info(f"ℹ️ {nb_saisis} sous-compteur(s) ont déjà des relevés pour la semaine '{semaine_label}'. Vous pouvez les modifier ci-dessous.")
                else:
                    st.info(f"📝 Aucune donnée enregistrée pour la semaine '{semaine_label}' (semaine vierge).")
            else:
                df_grid['Consommation'] = 0.0
                st.info(f"📝 Aucune donnée enregistrée pour la semaine '{semaine_label}' (semaine vierge).")

            edited_grid = st.data_editor(
                df_grid,
                column_config={
                    "compteur_id": None,
                    "Ordre": None,
                    "Bâtiment": st.column_config.TextColumn(disabled=True),
                    "Secteur": st.column_config.TextColumn(disabled=True),
                    "N° Compteur": st.column_config.TextColumn(disabled=True),
                    "Énergie": st.column_config.TextColumn(disabled=True),
                    "Unité": st.column_config.TextColumn(disabled=True),
                    "Relevé S-1 (Précédent)": st.column_config.NumberColumn("Relevé S-1 (Précédent)", disabled=True, format="%.1f"),
                    "Consommation": st.column_config.NumberColumn("Valeur / Index Conso", min_value=0.0, step=0.1)
                },
                hide_index=True, use_container_width=True, num_rows="fixed",
                key=f"grid_{semaine_label}_{secteur_filtre}"
            )

            c_btn1, c_btn2 = st.columns([2, 1])
            
            if c_btn1.button("💾 Enregistrer les relevés de cette tournée", type="primary"):
                count = 0
                with engine.begin() as conn:
                    for _, row in edited_grid.iterrows():
                        val = float(row['Consommation'])
                        if val > 0:
                            c_id = int(row['compteur_id'])
                            d_str = date_f.strftime("%Y-%m-%d")
                            
                            existing = conn.execute(
                                text("SELECT id FROM releves WHERE compteur_id = :cid AND semaine_label = :sem"),
                                {"cid": c_id, "sem": semaine_label}
                            ).fetchone()
                            
                            if existing:
                                conn.execute(text("""
                                    UPDATE releves 
                                    SET date_releve = :d_str, conso_val = :val, dju_reels = :dju
                                    WHERE id = :rid
                                """), {"d_str": d_str, "val": val, "dju": dju_val, "rid": existing[0]})
                            else:
                                conn.execute(text("""
                                    INSERT INTO releves (compteur_id, semaine_label, date_releve, conso_val, dju_reels)
                                    VALUES (:cid, :sem, :d_str, :val, :dju)
                                """), {"cid": c_id, "sem": semaine_label, "d_str": d_str, "val": val, "dju": dju_val})
                            count += 1
                if count > 0:
                    set_flash(f"Les relevés de {count} sous-compteur(s) ont été enregistrés avec succès dans Supabase pour la semaine '{semaine_label}' !", "success")
                else:
                    set_flash("Avertissement : Aucune valeur supérieure à 0 n'a été renseignée.", "error")
                st.rerun()

            df_export_hebdo = edited_grid[['Bâtiment', 'Secteur', 'N° Compteur', 'Énergie', 'Unité', 'Relevé S-1 (Précédent)', 'Consommation']].copy()
            c_btn2.download_button(
                label="📥 Exporter cette semaine au format Excel (.xlsx)",
                data=generate_excel_bytes(df_export_hebdo, sheet_name=f"Saisie_{semaine_label.split(' ')[0]}"),
                file_name=f"saisie_compteurs_{semaine_label.split(' ')[0]}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

# ==============================================================================
# TAB 4: GESTION DES BÂTIMENTS, SOUS-COMPTEURS & SECTEURS
# ==============================================================================
elif menu == "⚙️ Gestion Sites, Compteurs & Secteurs":
    st.markdown('<div class="main-header"><h2>⚙️ Administration du Parc</h2><span>Gestion des sites, édition des compteurs et réorganisation de l\'ordre des bâtiments</span></div>', unsafe_allow_html=True)
    display_flash()
    
    tab_add_site, tab_edit_site, tab_ordre_sites, tab_add_compteur, tab_edit_compteur, tab_secteurs, tab_list = st.tabs([
        "➕ Ajouter Bâtiment", "✏️ Modifier Site", "🔢 Ordre des Bâtiments",
        "➕ Ajouter Sous-Compteur", "✏️ Modifier Sous-Compteur", "🏷️ Renommer Secteurs", "📋 Liste Globale"
    ])

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
                if nom_bat.strip():
                    try:
                        with engine.begin() as conn:
                            conn.execute(text("""
                                INSERT INTO sites (nom, secteur, surface_m2, epoque, ordre)
                                VALUES (:nom, :sec, :surf, :epoque, :ordre)
                            """), {"nom": nom_bat.strip(), "sec": secteur_bat, "surf": float(surface_bat), "epoque": epoque_bat, "ordre": int(ordre_bat)})
                        set_flash(f"Le bâtiment '{nom_bat.strip()}' a été créé avec succès !", "success")
                        st.rerun()
                    except Exception as e:
                        st.error("Un bâtiment avec ce nom existe déjà.")
                else:
                    st.error("Le nom du bâtiment ne peut pas être vide.")

    with tab_edit_site:
        st.subheader("✏️ Modifier les caractéristiques, renommer ou supprimer un site")
        with engine.connect() as conn:
            sites_db = pd.read_sql("SELECT id, nom, secteur, surface_m2, epoque, ordre FROM sites ORDER BY ordre ASC, nom ASC", conn).to_dict('records')

        if not sites_db:
            st.info("Aucun bâtiment à modifier.")
        else:
            site_dict_edit = {f"{row['nom']} (Ordre: {row['ordre']})": int(row['id']) for row in sites_db}
            choix_site_edit = st.selectbox("Sélectionnez le bâtiment à modifier", list(site_dict_edit.keys()), key="select_edit_site_box")
            site_id_selected = site_dict_edit[choix_site_edit]

            with engine.connect() as conn:
                site_actuel = pd.read_sql("SELECT * FROM sites WHERE id = %(sid)s", conn, params={"sid": site_id_selected}).iloc[0]

            if site_actuel is not None:
                with st.form(key=f"form_edit_site_{site_id_selected}"):
                    nouveau_nom = st.text_input("Nouveau nom du Bâtiment", value=site_actuel['nom'])
                    nouveau_secteur = st.selectbox("Secteur", LISTE_SECTEURS, index=LISTE_SECTEURS.index(site_actuel['secteur']) if site_actuel['secteur'] in LISTE_SECTEURS else 0)
                    nouvelle_surface = st.number_input("Surface chauffée (m²)", min_value=10.0, value=float(site_actuel['surface_m2']))
                    nouvelle_epoque = st.selectbox("Époque / RT", list(REFERENTIEL_EPOQUES.keys()), index=list(REFERENTIEL_EPOQUES.keys()).index(site_actuel['epoque']) if site_actuel['epoque'] in REFERENTIEL_EPOQUES else 0)
                    nouvel_ordre = st.number_input("Ordre d'affichage (Numéro)", min_value=0, value=int(site_actuel['ordre'] if site_actuel['ordre'] else 0))
                    
                    submitted_save_site = st.form_submit_button("💾 Enregistrer les modifications", type="primary")
                    
                    if submitted_save_site:
                        if not nouveau_nom.strip():
                            st.error("Le nom ne peut pas être vide.")
                        else:
                            with engine.begin() as conn:
                                conn.execute(text("""
                                    UPDATE sites 
                                    SET nom = :nom, secteur = :sec, surface_m2 = :surf, epoque = :epoque, ordre = :ordre
                                    WHERE id = :sid
                                """), {"nom": nouveau_nom.strip(), "sec": nouveau_secteur, "surf": float(nouvelle_surface), "epoque": nouvelle_epoque, "ordre": int(nouvel_ordre), "sid": site_id_selected})
                            set_flash(f"Les modifications du bâtiment '{nouveau_nom.strip()}' ont été enregistrées avec succès !", "success")
                            st.rerun()

                st.divider()
                if st.button(f"🗑️ Supprimer définitivement le bâtiment '{site_actuel['nom']}'", key=f"btn_del_site_{site_id_selected}", type="secondary"):
                    with engine.begin() as conn:
                        conn.execute(text("DELETE FROM sites WHERE id = :sid"), {"sid": site_id_selected})
                    set_flash(f"Le bâtiment '{site_actuel['nom']}' et l'ensemble de ses sous-compteurs ont été supprimés définitivement !", "info")
                    st.rerun()

    with tab_ordre_sites:
        st.subheader("🔢 Organiser l'ordre des Bâtiments")
        with engine.connect() as conn:
            df_ordre_sites = pd.read_sql("SELECT id, nom as \"Bâtiment\", secteur as \"Secteur\", ordre as \"Ordre\" FROM sites ORDER BY ordre ASC, nom ASC", conn)

        if df_ordre_sites.empty:
            st.info("Aucun bâtiment dans la base.")
        else:
            edited_ordre_grid = st.data_editor(
                df_ordre_sites,
                column_config={
                    "id": None,
                    "Bâtiment": st.column_config.TextColumn(disabled=True),
                    "Secteur": st.column_config.TextColumn(disabled=True),
                    "Ordre": st.column_config.NumberColumn("Ordre d'affichage (ex: 1, 2, 3...)", min_value=0, step=1)
                },
                hide_index=True, use_container_width=True, key="grid_reordre_sites"
            )

            if st.button("💾 Enregistrer le nouvel ordre des bâtiments", type="primary"):
                with engine.begin() as conn:
                    for _, row in edited_ordre_grid.iterrows():
                        conn.execute(text("UPDATE sites SET ordre = :o WHERE id = :sid"), {"o": int(row['Ordre']), "sid": int(row['id'])})
                set_flash("L'ordre d'affichage des bâtiments a été mis à jour avec succès !", "success")
                st.rerun()

    with tab_add_compteur:
        st.subheader("➕ Rattacher un sous-compteur à un bâtiment")
        with engine.connect() as conn:
            sites_for_compteurs = pd.read_sql("SELECT id, nom, secteur FROM sites ORDER BY ordre ASC, nom ASC", conn).to_dict('records')

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
                    num_c = st.text_input("Numéro du sous-compteur (ex: GAZ-SUB-01)")
                    type_e = st.selectbox("Type d'énergie", ["Gaz naturel", "Chauffage urbain", "Électricité"])
                    unite_c = st.selectbox("Unité de mesure", ["m3", "MWh", "kWh", "kW"])
                    
                    if st.form_submit_button("Ajouter le sous-compteur", type="primary"):
                        if num_c.strip():
                            target_site_id = site_dict_add[sel_site_label]
                            try:
                                with engine.begin() as conn:
                                    conn.execute(text("""
                                        INSERT INTO compteurs (site_id, numero_compteur, type_energie, unite)
                                        VALUES (:sid, :num, :type_e, :unite)
                                    """), {"sid": target_site_id, "num": num_c.strip(), "type_e": type_e, "unite": unite_c})
                                set_flash(f"Le sous-compteur '{num_c.strip()}' a été ajouté avec succès !", "success")
                                st.rerun()
                            except Exception:
                                st.error("Ce numéro de compteur existe déjà dans la base.")
                        else:
                            st.error("Le numéro de sous-compteur ne peut pas être vide.")

    with tab_edit_compteur:
        st.subheader("✏️ Modifier, Réattribuer ou Supprimer un Sous-Compteur")
        with engine.connect() as conn:
            compteurs_db = pd.read_sql("""
                SELECT c.id, c.site_id, c.numero_compteur, c.type_energie, c.unite, s.nom as site_nom, s.secteur 
                FROM compteurs c JOIN sites s ON c.site_id = s.id 
                ORDER BY s.ordre ASC, s.nom ASC, c.numero_compteur ASC
            """, conn).to_dict('records')
            all_sites_db = pd.read_sql("SELECT id, nom, secteur FROM sites ORDER BY ordre ASC, nom ASC", conn).to_dict('records')

        if not compteurs_db:
            st.info("Aucun sous-compteur enregistré.")
        else:
            compteur_dict_edit = {f"{r['site_nom']} ➔ {r['numero_compteur']} ({r['type_energie']})": int(r['id']) for r in compteurs_db}
            choix_c_edit = st.selectbox("Sélectionnez le sous-compteur à modifier", list(compteur_dict_edit.keys()), key="select_edit_compteur_box")
            compteur_id_selected = compteur_dict_edit[choix_c_edit]

            with engine.connect() as conn:
                c_actuel = pd.read_sql("SELECT * FROM compteurs WHERE id = %(cid)s", conn, params={"cid": compteur_id_selected}).iloc[0]

            if c_actuel is not None:
                site_options = {f"{s['nom']} ({s['secteur']})": int(s['id']) for s in all_sites_db}
                current_site_id = int(c_actuel['site_id'])
                current_site_label = [k for k, v in site_options.items() if v == current_site_id]
                default_site_idx = list(site_options.keys()).index(current_site_label[0]) if current_site_label else 0

                with st.form(key=f"form_edit_compteur_{compteur_id_selected}"):
                    nouveau_site_label = st.selectbox("🏢 Bâtiment rattaché (Réattribution)", list(site_options.keys()), index=default_site_idx)
                    nouveau_site_id = site_options[nouveau_site_label]
                    nouveau_num = st.text_input("Numéro du sous-compteur", value=c_actuel['numero_compteur'])
                    energies_possibles = ["Gaz naturel", "Chauffage urbain", "Électricité"]
                    nouveau_type = st.selectbox("Type d'énergie", energies_possibles, index=energies_possibles.index(c_actuel['type_energie']) if c_actuel['type_energie'] in energies_possibles else 0)
                    unites_possibles = ["m3", "MWh", "kWh", "kW"]
                    nouvelle_unite = st.selectbox("Unité de mesure", unites_possibles, index=unites_possibles.index(c_actuel['unite']) if c_actuel['unite'] in unites_possibles else 0)
                    
                    submitted_save_compteur = st.form_submit_button("💾 Enregistrer les modifications", type="primary")
                    
                    if submitted_save_compteur:
                        if not nouveau_num.strip():
                            st.error("Le numéro de sous-compteur ne peut pas être vide.")
                        else:
                            with engine.begin() as conn:
                                conn.execute(text("""
                                    UPDATE compteurs 
                                    SET site_id = :sid, numero_compteur = :num, type_energie = :te, unite = :unite
                                    WHERE id = :cid
                                """), {"sid": nouveau_site_id, "num": nouveau_num.strip(), "te": nouveau_type, "unite": nouvelle_unite, "cid": compteur_id_selected})
                            set_flash(f"Le sous-compteur '{nouveau_num.strip()}' a été mis à jour avec succès !", "success")
                            st.rerun()

                st.divider()
                if st.button("🗑️ Supprimer définitivement ce sous-compteur", key=f"btn_del_compteur_{compteur_id_selected}", type="secondary"):
                    with engine.begin() as conn:
                        conn.execute(text("DELETE FROM compteurs WHERE id = :cid"), {"cid": compteur_id_selected})
                    set_flash("Le sous-compteur a été supprimé définitivement !", "info")
                    st.rerun()

    with tab_secteurs:
        st.subheader("🏷️ Personnaliser et renommer les 7 secteurs")
        with engine.connect() as conn:
            secteurs_rows = pd.read_sql("SELECT id, nom FROM secteurs ORDER BY id", conn).to_dict('records')

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
                    st.error("Aucun nom de secteur ne peut être vide.")
                elif len(all_new_names) != len(set(all_new_names)):
                    st.error("Tous les noms de secteurs doivent être uniques.")
                else:
                    with engine.begin() as conn:
                        for sec_id, (old_name, new_name) in nouveaux_noms_map.items():
                            if old_name != new_name:
                                conn.execute(text("UPDATE secteurs SET nom = :n WHERE id = :sid"), {"n": new_name, "sid": sec_id})
                                conn.execute(text("UPDATE sites SET secteur = :n WHERE secteur = :old"), {"n": new_name, "old": old_name})
                    set_flash("Tous les secteurs ont été renommés avec succès !", "success")
                    st.rerun()

    with tab_list:
        with engine.connect() as conn:
            df_all = pd.read_sql("""
                SELECT s.ordre as Ordre, s.nom as Bâtiment, s.secteur as Secteur, s.surface_m2 as Surface, s.epoque as 'Époque RT',
                       c.numero_compteur as 'N° Compteur', c.type_energie as Énergie, c.unite as Unité
                FROM sites s LEFT JOIN compteurs c ON s.id = c.site_id
                ORDER BY s.ordre ASC, s.nom ASC
            """, conn)
        
        col_l1, col_l2 = st.columns([3, 1])
        col_l1.subheader("📋 Répertoire complet du parc")
        col_l2.download_button(
            label="📥 Exporter tout le parc en Excel",
            data=generate_excel_bytes(df_all, sheet_name="Parc_Complet"),
            file_name="parc_batiments_compteurs.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
        st.dataframe(df_all, hide_index=True, use_container_width=True)