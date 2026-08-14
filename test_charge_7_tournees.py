import os
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from sqlalchemy import create_engine, text

# ==============================================================================
# RÉCUPÉRATION AUTOMATIQUE DE L'URL SUPABASE DEPUIS SECRETS.TOML
# ==============================================================================
def load_db_url():
    """Lit automatiquement l'URL de connexion dans .streamlit/secrets.toml"""
    secrets_path = os.path.join(".streamlit", "secrets.toml")
    if os.path.exists(secrets_path):
        try:
            import tomllib  # Python 3.11+
            with open(secrets_path, "rb") as f:
                secrets = tomllib.load(f)
                if "db" in secrets and "url" in secrets["db"]:
                    return secrets["db"]["url"]
        except Exception:
            try:
                import toml
                secrets = toml.load(secrets_path)
                if "db" in secrets and "url" in secrets["db"]:
                    return secrets["db"]["url"]
            except Exception:
                pass
    
    # URL de secours par défaut si le fichier secrets.toml est introuvable
    return "postgresql://postgres:VOTRE_MOT_DE_PASSE@db.xxx.supabase.co:5432/postgres"

DB_URL = load_db_url()
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# Création du moteur de connexion avec pool dimensionné pour les tests simultanés
engine = create_engine(
    DB_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=5,
    pool_timeout=30,
    pool_recycle=1800,
)

# ==============================================================================
# FONCTIONS DE SIMULATION DYNAMIQUES
# ==============================================================================
def get_secteurs_actifs():
    """Récupère dynamiquement la liste exacte des secteurs en BDD."""
    with engine.connect() as conn:
        res = conn.execute(text("SELECT nom FROM secteurs ORDER BY id")).fetchall()
        return [r[0] for r in res]

def simuler_tournee_agent(secteur_nom: str, semaine_label: str):
    """
    Simule un agent validant simultanément les index de sa tournée.
    """
    start_time = time.time()
    result = {"secteur": secteur_nom, "success": False, "nb_compteurs": 0, "duration": 0, "error": None}
    
    try:
        # Récupération des sous-compteurs rattachés à ce secteur
        with engine.connect() as conn:
            compteurs = conn.execute(text("""
                SELECT c.id FROM compteurs c
                JOIN sites s ON c.site_id = s.id
                WHERE s.secteur = :sec
            """), {"sec": secteur_nom}).fetchall()
            
        if not compteurs:
            result["error"] = "Aucun compteur rattaché à ce secteur."
            result["duration"] = round(time.time() - start_time, 3)
            return result

        # Écriture simultanée des relevés en base (UPSERT SQL)
        with engine.begin() as conn:
            for c in compteurs:
                compteur_id = c[0]
                index_simule = round(random.uniform(50.0, 3500.0), 1)
                
                conn.execute(text("""
                    INSERT INTO releves (compteur_id, semaine_label, date_releve, conso_val, dju_reels, dju_fiable)
                    VALUES (:cid, :sem, :dt, :val, :dju, TRUE)
                    ON CONFLICT (compteur_id, semaine_label) DO UPDATE
                    SET conso_val = EXCLUDED.conso_val, date_releve = EXCLUDED.date_releve;
                """), {
                    "cid": compteur_id,
                    "sem": semaine_label,
                    "dt": date.today().strftime("%Y-%m-%d"),
                    "val": index_simule,
                    "dju": 12.5
                })
                
        result["success"] = True
        result["nb_compteurs"] = len(compteurs)
    except Exception as e:
        result["error"] = str(e)
        
    result["duration"] = round(time.time() - start_time, 3)
    return result

# ==============================================================================
# EXÉCUTION DU TEST
# ==============================================================================
def lancer_test_charge():
    print("⚡ [TEST DE CHARGE SIMULTANÉ] Connexion à Supabase...")
    
    try:
        secteurs_actuels = get_secteurs_actifs()
    except Exception as e:
        print(f"\n🚨 Impossible de se connecter à la BDD.")
        print(f"Raison : {e}")
        return

    if not secteurs_actuels:
        print("⚠️ Aucun secteur trouvé en base de données.")
        return

    nb_bots = len(secteurs_actuels)
    today_date = date.today()
    semaine_test = f"S{today_date.isocalendar()[1]:02d} (TEST CHARGE)"

    print(f"🚀 Lancement de {nb_bots} bot(s) en simultané (1 bot par tournée détectée)...")
    print(f"📅 Semaine ciblée : {semaine_test}\n")
    
    start_total = time.time()
    results = []
    
    # Exécution simultanée via ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=nb_bots) as executor:
        futures = [executor.submit(simuler_tournee_agent, sec, semaine_test) for sec in secteurs_actuels]
        for future in as_completed(futures):
            results.append(future.result())
            
    total_time = round(time.time() - start_total, 2)
    succes = sum(1 for r in results if r["success"])
    echecs = sum(1 for r in results if not r["success"])
    
    print("=" * 70)
    print(f"📊 RÉSULTAT DU TEST DE CHARGE SIMULTANÉ")
    print("=" * 70)
    print(f"⏱️ Temps d'exécution total : {total_time}s")
    print(f"✅ Tournées validées       : {succes}/{nb_bots}")
    print(f"🚨 Échecs / Secteurs vides  : {echecs}/{nb_bots}\n")
    
    print("Détail par tournée :")
    for r in sorted(results, key=lambda x: x["secteur"]):
        statut = "✅ PASS" if r["success"] else "🚨 FAIL"
        info = f"{r['nb_compteurs']} compteur(s) mis à jour" if r["success"] else f"Raison : {r['error']}"
        print(f"  {statut} | {r['secteur'][:35]:<35} | {r['duration']:>5}s | {info}")
    print("=" * 70)

if __name__ == "__main__":
    lancer_test_charge()
