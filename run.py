import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import uvicorn
if __name__=='__main__':
    import os
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent/'.env')
    if '--enable-approved-sending' in sys.argv:os.environ['SALES_AX_SEND_ENABLED']='true'
    uvicorn.run('ax.api:app',host='127.0.0.1',port=8765,access_log=False)
