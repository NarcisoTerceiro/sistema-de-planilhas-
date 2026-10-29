"""Use o mesmo comando anterior para iniciar o sistema com a integração."""
from servidor_online import app
import os

if __name__ == '__main__':
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '5567'))
    print(f'Data Formulator: http://{host}:{port}')
    print(f'WhatsApp: http://{host}:{port}/integracoes/whatsapp')
    app.run(host=host, port=port, threaded=True, use_reloader=False)
