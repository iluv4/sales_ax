import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import uvicorn
if __name__=='__main__':
    import os
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent/'.env')
    if '--enable-approved-sending' in sys.argv:os.environ['SALES_AX_SEND_ENABLED']='true'
    # Only the password-protected hosted demo listens beyond loopback.
    hosted=bool(os.getenv('SALES_AX_PUBLIC_HOST'))
    uvicorn.run('ax.api:app',host='0.0.0.0' if hosted else '127.0.0.1',port=int(os.getenv('PORT','8765')) if hosted else 8765,
                access_log=False,proxy_headers=hosted)
