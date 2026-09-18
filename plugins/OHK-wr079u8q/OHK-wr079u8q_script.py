"""
This is a working sample CloudBolt plug-in for you to start with. The run method is required,
but you can change all the code within it. See the "CloudBolt Plug-ins" section of the docs for
more info and the CloudBolt forge for more examples:
https://github.com/CloudBoltSoftware/cloudbolt-forge/tree/master/actions/cloudbolt_plugins
"""
from common.methods import set_progress


def run(job, server=None, *args, **kwargs):
    script = get_script(server)
    response = server.execute_script(script_contents=script)
    set_progress(f'Postgres server successfully connected. Output: {response}')
    return "SUCCESS", "", ""
        

def get_script(server):
    return f"""#!/usr/bin/env bash
PGPASSWORD='{server.postgres_database_password}' psql -h {server.hostname} -p 5432 -U {server.postgres_database_owner} -d {server.postgres_database_name} -c "SELECT version(), current_database(), current_user, now();"
"""