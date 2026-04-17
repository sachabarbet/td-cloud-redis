import os
import json
import threading
from datetime import datetime, timezone
import redis
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from google.cloud import pubsub_v1

app = Flask(__name__)
# On initialise SocketIO avec eventlet (requis par le TD)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# 1. Configuration Redis
r = redis.Redis(
    host=os.environ.get("REDIS_HOST", "127.0.0.1"),
    port=int(os.environ.get("REDIS_PORT", 6379)),
    decode_responses=True
)

# 2. Configuration GCP Pub/Sub
PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
TOPIC_NAME = os.environ.get("TOPIC_NAME")
SUBSCRIPTION_NAME = os.environ.get("SUBSCRIPTION_NAME")

publisher = pubsub_v1.PublisherClient()
subscriber = pubsub_v1.SubscriberClient()
topic_path = publisher.topic_path(PROJECT_ID, TOPIC_NAME)
subscription_path = subscriber.subscription_path(PROJECT_ID, SUBSCRIPTION_NAME)

SERVER_ID = os.environ.get("HOSTNAME", "local")

# --- LOGIQUE PUB/SUB (Background) ---

def pubsub_callback(message):
    """Fonction appelée à chaque message reçu depuis GCP Pub/Sub"""
    try:
        # Le message contient la clé Redis
        redis_key = message.data.decode("utf-8")
        
        # On lit la donnée fraîche dans Redis 
        value = r.get(redis_key)
        if value:
            data = json.loads(value)
            # On "pousse" l'update à tous les clients WebSocket de CETTE instance 
            socketio.emit("update", {"key": redis_key, "data": data})
        
        # Accusé de réception pour supprimer le message de la queue 
        message.ack()
    except Exception as e:
        print(f"Erreur callback Pub/Sub: {e}")

def start_pubsub_listener():
    """Lance l'écoute Pub/Sub en streaming dans un thread séparé """
    streaming_pull_future = subscriber.subscribe(subscription_path, callback=pubsub_callback)
    print(f"Listening for messages on {subscription_path}...")
    try:
        streaming_pull_future.result()
    except Exception as e:
        streaming_pull_future.cancel()
        print(f"Pub/Sub listener arrêté: {e}")

# --- LOGIQUE WEBSOCKET ---

@socketio.on("connect")
def handle_connect():
    """À la connexion d'un client, on envoie l'état initial [cite: 508-509]"""
    print(f"Client connecté à l'instance {SERVER_ID}")
    
    result = {}
    cursor = 0
    # On scanne Redis pour récupérer tous les événements actuels 
    while True:
        cursor, keys = r.scan(cursor=cursor, match="event:*", count=100)
        for key in keys:
            value = r.get(key)
            if value:
                result[key] = json.loads(value)
        if cursor == 0:
            break
            
    # Envoi immédiat de l'état complet au client qui vient de se connecter 
    emit("initial_state", {"server_id": SERVER_ID, "entries": result})

# --- ROUTES API ---

@app.route("/publish", methods=["POST"])
def publish():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify({"error": "Champ 'message' requis"}), 400

    entry = {
        "message": data["message"],
        "server_id": SERVER_ID,
        "published_at": datetime.now(timezone.utc).isoformat()
    }

    # A. Écriture dans Redis (Persistance) [cite: 515]
    key = f"event:{SERVER_ID}:{entry['published_at']}"
    r.setex(key, 3600, json.dumps(entry))
    
    # B. Publication de la CLÉ sur le topic Pub/Sub (Notification) [cite: 515]
    # On ne publie pas la donnée entière, seulement la clé pour forcer les instances à lire Redis
    publisher.publish(topic_path, key.encode("utf-8"))
    
    return jsonify({"status": "published", "redis_key": key})

@app.route("/health")
def health():
    try:
        r.ping()
        return jsonify({"status": "healthy", "server_id": SERVER_ID, "redis": "connected"})
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 503

if __name__ == "__main__":
    # Lancement du thread Pub/Sub au démarrage 
    pubsub_thread = threading.Thread(target=start_pubsub_listener, daemon=True)
    pubsub_thread.start()
    
    # Lancement du serveur via socketio au lieu de app.run
    port = int(os.environ.get("PORT", 8080))
    socketio.run(app, host="0.0.0.0", port=port)