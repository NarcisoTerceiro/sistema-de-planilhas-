"""Abre o Data Formulator completo com a extensão de WhatsApp instalada."""
import os
from servidor_online import app

if __name__ == '__main__':
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '5567'))
    print(f'Sistema principal: http://{host}:{port}')
    print(f'Teste da extensão: http://{host}:{port}/integracoes/whatsapp')
    app.run(host=host, port=port, threaded=True, use_reloader=False)
