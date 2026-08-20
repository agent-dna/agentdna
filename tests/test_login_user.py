import agentdna
print("using agentdna from:", agentdna.__file__)
user=agentdna.login(skip_actor_id_registration=True)
print("name (email):", user.name)
print("type:", user.type)
print("run_id:", user.run_id)
print("api_key:", user.api_key)