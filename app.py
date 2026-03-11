# APPLICATION STREAMLIT - SURVEILLANCE FORESTIÈRE GABON

import streamlit as st
import ee
import folium
from streamlit_folium import st_folium
from datetime import datetime

# AUTHENTIFICATION EARTH ENGINE - VERSION STREAMLIT CLOUD

import json

@st.cache_resource
def init_ee():
    """Initialise Earth Engine avec Service Account pour Streamlit Cloud"""
    
    try:
        # Essayer de lire les secrets (Streamlit Cloud)
        if st.secrets.get('earthengine'):
            service_account_key = st.secrets['earthengine']['service_account_key']
            project_id = st.secrets['earthengine']['project_id']
            
            # Créer les credentials depuis la clé JSON
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_info(
                json.loads(service_account_key),
                scopes=['https://www.googleapis.com/auth/earthengine']
            )
            
            ee.Initialize(credentials=credentials, project=project_id)
            st.success("Connecté à Earth Engine (Cloud)")
            return True
        else:
            # Fallback pour test local
            ee.Initialize(project='conservation-projet')
            st.success("Connecté à Earth Engine (Local)")
            return True
            
    except Exception as e:
        st.error(f"Erreur de connexion Earth Engine : {e}")
        st.info("Vérifiez les secrets dans Streamlit Cloud")
        return False

# Initialiser au démarrage
if 'ee_initialized' not in st.session_state:
    st.session_state.ee_initialized = init_ee()

if not st.session_state.ee_initialized:
    st.stop()
# CACHE : Initialisation Earth Engine (exécutée une seule fois)

@st.cache_resource
def init_ee(project_id):
    """Initialise Earth Engine une seule fois par session"""
    ee.Initialize(project=project_id)
    return True

# CACHE : Fonction d'analyse (évite de recalculer à chaque interaction)

@st.cache_data(ttl=3600)  # Cache pendant 1 heure
def run_analysis(lon, lat, rayon, max_nuages, date_ref_debut, date_ref_fin, 
                 date_act_debut, date_act_fin, seuil_alerte):
    """
    Exécute l'analyse Earth Engine et retourne les résultats.
    Les résultats sont mis en cache pour éviter de recalculer à chaque clic.
    """
    # Zone d'intérêt
    roi = ee.Geometry.Point([lon, lat]).buffer(rayon * 1000)
    
    # Collection Sentinel-2
    collection = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
        .filterBounds(roi) \
        .filterDate(str(date_ref_debut), str(date_act_fin)) \
        .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', max_nuages))
    
    nombre_total = collection.size().getInfo()
    
    if nombre_total == 0:
        return {'error': 'Aucune image disponible'}
    
    # Masque nuages
    def maskS2(image):
        qa = image.select('QA60')
        mask = qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0))
        return image.updateMask(mask).divide(10000)
    
    collection = collection.map(maskS2)
    
    # NDVI
    def add_ndvi(img):
        return img.addBands(img.normalizedDifference(['B8', 'B4']).rename('NDVI'))
    
    collection = collection.map(add_ndvi)
    
    # Composites
    collection_ref = collection.filterDate(str(date_ref_debut), str(date_ref_fin))
    collection_act = collection.filterDate(str(date_act_debut), str(date_act_fin))
    
    n_ref = collection_ref.size().getInfo()
    n_act = collection_act.size().getInfo()
    
    if n_ref == 0 or n_act == 0:
        ndvi_ref = collection.select('NDVI').first().clip(roi)
        ndvi_act = collection.select('NDVI').sort('system:time_start', False).first().clip(roi)
    else:
        ndvi_ref = collection_ref.select('NDVI').median().clip(roi)
        ndvi_act = collection_act.select('NDVI').median().clip(roi)
    
    # Détection coupes
    changement = ndvi_act.subtract(ndvi_ref)
    zones_coupe = changement.lt(seuil_alerte).selfMask()
    
    # Statistiques
    stats_surface = zones_coupe.multiply(ee.Image.pixelArea()).reduceRegion(
        reducer=ee.Reducer.sum(), geometry=roi, scale=10, maxPixels=1e9)
    surface_ha = ee.Number(stats_surface.get('NDVI')).divide(10000).getInfo() or 0
    
    stats_ndvi = ndvi_act.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=10, maxPixels=1e9)
    ndvi_moyen = ee.Number(stats_ndvi.get('NDVI')).getInfo() or 0
    
    # Retourner tous les résultats
    return {
        'roi': roi,
        'ndvi_act': ndvi_act,
        'zones_coupe': zones_coupe,
        'surface_ha': surface_ha,
        'ndvi_moyen': ndvi_moyen,
        'n_images': nombre_total,
        'n_ref': n_ref,
        'n_act': n_act,
        'lon': lon,
        'lat': lat,
        'rayon': rayon
    }


# FONCTION : Créer la carte Folium

def create_folium_map(results, nom_parc):
    """Crée la carte interactive avec folium"""
    
    lat = results['lat']
    lon = results['lon']
    roi = results['roi']
    ndvi_act = results['ndvi_act']
    zones_coupe = results['zones_coupe']
    
    # Créer carte folium
    m = folium.Map(location=[lat, lon], zoom_start=10, tiles=None)
    
    # Fond satellite Esri
    folium.TileLayer(
        tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        attr='Esri Satellite',
        name='Satellite',
        overlay=False,
        control=True
    ).add_to(m)
    
    # Couches Earth Engine
    try:
        # NDVI
        vis_ndvi = {'min': 0, 'max': 0.8, 'palette': ['red', 'yellow', 'lightgreen', 'green', 'darkgreen']}
        map_id_ndvi = ndvi_act.getMapId(vis_ndvi)
        url_ndvi = map_id_ndvi['tile_fetcher'].url_format
        
        folium.TileLayer(
            tiles=url_ndvi,
            attr='Google Earth Engine - NDVI',
            name='NDVI Actuel',
            overlay=True,
            control=True,
            opacity=0.7
        ).add_to(m)
        
        # Zones de coupe
        vis_coupe = {'min': 0, 'max': 1, 'palette': ['transparent', 'red']}
        map_id_coupe = zones_coupe.getMapId(vis_coupe)
        url_coupe = map_id_coupe['tile_fetcher'].url_format
        
        folium.TileLayer(
            tiles=url_coupe,
            attr='Google Earth Engine - Alertes',
            name='Zones de Coupe',
            overlay=True,
            control=True,
            opacity=0.9
        ).add_to(m)
        
    except Exception as e:
        print(f"Erreur couches GEE : {e}")
    
    # Zone d'étude
    folium.GeoJson(
        roi.getInfo(),
        style_function=lambda x: {'color': 'blue', 'weight': 2, 'fillOpacity': 0},
        name='Zone d étude'
    ).add_to(m)
    
    # Marqueur
    folium.Marker(
        location=[lat, lon],
        popup=f"{nom_parc}<br>{lat}°S, {lon}°E",
        icon=folium.Icon(color='green', icon='tree-conifer', prefix='fa')
    ).add_to(m)
    
    # Légende
    legend_html = '''
    <div style="position: fixed; bottom: 50px; right: 50px; width: 220px; height: 140px; 
                background-color: white; border:2px solid grey; z-index:9999; font-size:12px; 
                padding: 10px; border-radius: 5px; box-shadow: 2px 2px 5px rgba(0,0,0,0.3)">
        <b>NDVI - Santé végétation</b><br><hr style="margin:5px 0">
        <span style="color:#FF0000">■</span> 0.0-0.2 : Sol/Eau<br>
        <span style="color:#FFA500">■</span> 0.2-0.4 : Vég. faible<br>
        <span style="color:#90EE90">■</span> 0.4-0.6 : Modéré<br>
        <span style="color:#008000">■</span> 0.6-0.8 : Bon<br>
        <span style="color:#006400">■</span> 0.8+ : Excellent
    </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))
    
    # Contrôles
    folium.LayerControl().add_to(m)
    
    return m

# INTERFACE STREAMLIT

# Configuration page
st.set_page_config(page_title="Surveillance Forêt Gabon", layout="wide", page_icon="🇬🇦")

# Titre
st.title("Surveillance Forestière - Gabon")
st.markdown("*Détection de coupes illégales et suivi de santé forestière*")
st.markdown("*Données : Sentinel-2 | Traitement : Google Earth Engine*")


# AUTHENTIFICATION EARTH ENGINE (une fois par session)

if 'ee_initialized' not in st.session_state:
    st.session_state.ee_initialized = False

if not st.session_state.ee_initialized:
    with st.spinner('Connexion à Earth Engine...'):
        try:
            init_ee('conservation-projet')
            st.session_state.ee_initialized = True
            st.success("Connecté à Earth Engine")
        except Exception as e:
            st.error(f"Erreur de connexion : {e}")
            st.info("Exécutez `earthengine authenticate` dans le terminal")
            st.stop()


# BARRE LATÉRALE : PARAMÈTRES

st.sidebar.header("Paramètres")

# Sélecteur de parc
parcs = {
    'Lopé': {'lon': 11.6, 'lat': -0.2},
    'Ivindo': {'lon': 12.8, 'lat': 0.5},
    'Minkébé': {'lon': 13.5, 'lat': 2.5},
    'Loango': {'lon': 10.3, 'lat': -1.0},
    'Mayumba': {'lon': 10.6, 'lat': -2.8},
}

nom_parc = st.sidebar.selectbox("Parc National", list(parcs.keys()))
lon = parcs[nom_parc]['lon']
lat = parcs[nom_parc]['lat']

rayon = st.sidebar.slider("Rayon (km)", 5, 50, 15)
max_nuages = st.sidebar.slider("Nuages max (%)", 20, 60, 40)
seuil_alerte = st.sidebar.slider("Seuil alerte NDVI", -0.5, -0.1, -0.25, 0.05)

col1, col2 = st.sidebar.columns(2)
with col1:
    date_ref_debut = st.date_input("Réf. Début", datetime(2022, 6, 1))
    date_ref_fin = st.date_input("Réf. Fin", datetime(2023, 5, 31))
with col2:
    date_act_debut = st.date_input("Actuel Début", datetime(2025, 6, 1))
    date_act_fin = st.date_input("Actuel Fin", datetime(2026, 2, 28))

# BOUTON D'ANALYSE (avec session_state pour persistance)

# Initialiser session_state pour les résultats
if 'analysis_results' not in st.session_state:
    st.session_state.analysis_results = None
if 'analysis_ran' not in st.session_state:
    st.session_state.analysis_ran = False

# Bouton pour lancer l'analyse
if st.sidebar.button("Lancer l'analyse", type="primary"):
    with st.spinner('Traitement des images satellites...'):
        try:
            # Exécuter l'analyse (résultats mis en cache)
            results = run_analysis(
                lon, lat, rayon, max_nuages,
                date_ref_debut, date_ref_fin,
                date_act_debut, date_act_fin,
                seuil_alerte
            )
            
            if results.get('error'):
                st.error(f"{results['error']}")
                st.warning("Élargir les dates, augmenter nuages max, ou agrandir le rayon")
            else:
                # Sauvegarder les résultats dans session_state
                st.session_state.analysis_results = results
                st.session_state.analysis_ran = True
                st.session_state.nom_parc = nom_parc
                st.success("Analyse terminée !")
                
        except Exception as e:
            st.error(f"Erreur : {e}")
            st.info("Vérifiez les dates, le pourcentage de nuages et le rayon")

# AFFICHAGE DES RÉSULTATS (si analyse effectuée)

if st.session_state.analysis_ran and st.session_state.analysis_results:
    
    results = st.session_state.analysis_results
    nom_parc = st.session_state.nom_parc
    
    # Informations sur les images
    st.info(f"{results['n_images']} images Sentinel-2 traitées")
    st.write(f"Référence : {results['n_ref']} images | Actuelle : {results['n_act']} images")
    
    # Indicateurs principaux
    col1, col2, col3 = st.columns(3)
    col1.metric("NDVI Moyen", f"{results['ndvi_moyen']:.3f}")
    col2.metric("Surface Affectée", f"{results['surface_ha']:.2f} ha")
    
    if results['ndvi_moyen'] > 0.7:
        etat = "Excellent"
    elif results['ndvi_moyen'] > 0.5:
        etat = "Bon"
    else:
        etat = "Dégradé"
    col3.metric("État de la forêt", etat)
    
    # CARTE INTERACTIVE (avec key unique pour éviter re-renders)
    
    st.subheader("Carte de Surveillance")
    
    # Créer la carte
    m = create_folium_map(results, nom_parc)
    
    # Afficher avec key unique (IMPORTANT pour la persistance)
    map_key = f"map_{nom_parc}_{results['lat']}_{results['lon']}"
    st_folium(m, width=800, height=600, key=map_key)
    
    
    # INTERPRÉTATION DES RÉSULTATS
   
    st.subheader("Interprétation")
    
    if results['surface_ha'] > 50:
        st.error("**ALERTE ROUGE :** Déforestation importante détectée.")
        st.info("Action recommandée : Patrouille immédiate sur les coordonnées détectées")
    elif results['surface_ha'] > 10:
        st.warning("**ALERTE JAUNE :** Perturbations détectées.")
        st.info("Action recommandée : Surveillance renforcée sous 48h")
    else:
        st.success("**Aucune alerte majeure.** Surveillance routinière.")
    
    # Conseils d'interprétation NDVI
    with st.expander("Guide d'interprétation du NDVI"):
        st.markdown("""
        | NDVI | Couleur | Signification |
        |------|---------|---------------|
        | 0.0-0.2 | 🔴 Rouge | Sol nu, eau, zone dégradée |
        | 0.2-0.4 | 🟠 Orange | Végétation faible, savane |
        | 0.4-0.6 | 🟡 Jaune-Vert | Forêt secondaire, modérée |
        | 0.6-0.8 | 🟢 Vert | Forêt saine |
        | 0.8+ | 🟢🟢 Vert foncé | Forêt primaire dense |
        """)


# MESSAGE SI AUCUNE ANALYSE LANCÉE

else:
    st.info("Sélectionnez un parc et cliquez sur **Lancer l'analyse** pour commencer")
    
    # Aperçu rapide
    with st.expander("Comment utiliser cette application ?"):
        st.markdown("""
        1. **Sélectionnez un parc** dans la liste déroulante
        2. **Ajustez les paramètres** si besoin (rayon, dates, seuil d'alerte)
        3. **Cliquez sur "Lancer l'analyse"**
        4. **Consultez les résultats** : indicateurs, carte, interprétation
        5. **Exportez ou partagez** les alertes avec votre équipe terrain
        """)

# PIED DE PAGE

st.markdown("---")
st.caption("Développé pour la conservation de la faune sauvage au Gabon 🇬🇦 | Données : Copernicus Sentinel-2"
           " | Traitement : Google Earth Engine | Interface : Streamlit by Dieudonné Gabla")



