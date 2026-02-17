# Github MCP Demo

This demo represents the use of `agentdna` package for a simple Agent to Github MCP Architecture.

## Install Dependencies

- Setup a Python virtual environment

    - Inside the Github MCP example directory, run the following to boostrap virtual environment files under the directory `venv`:
        ```
        # Use `python3`, if `python` is configured as Python2
        python -m venv venv
        ```
    
    - Activate the virtual environment.
        
        Windows (Powershell):
        ```
        .\venv\Scripts\Activate.ps1
        ```

        Unix (Ubuntu/Mac OS):
        ```
        # Provide permission to the activate script
        chmod +x ./venv/bin/activate 
        ./venv/bin/activate
        ```

- Run the following to install dependencies:

    ```
    pip install -r requirements.txt
    ```

    Some systems have both `pip` and `pip3` representing Python2 and Python3 respectively. To verify, run `pip --version` and check the Python's version. Since this project relies in Python3, if `pip --version` shows Python2, consider using `pip3` to install dependencies. In such instances, consider using `python3` CLI in any of the further instructions where `python` CLI is mentioned

## Start the Demo

- Create the `.env` file:
    ```
    cp .env.sample .env
    ```

    Set the environment variables accordingly. Following are the environment variables:
    ```
        AGENTDNA_API_KEY=AgentDNA API Key. Get it from here: https://agentdna.io/beta
        GEMINI_API_KEY=Gemini API Key
        GITHUB_TOKEN=Github Personal Access Token
        HOST_AGENT_NAME=Name for the Github Agent
        MCP_TOOL_NAME=Name for the Github MCP Tool
    ```

- Run the following to start the demo:
    ```
    python -m streamlit run app.py
    ```
