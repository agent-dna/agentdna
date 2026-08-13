import time

from run import main

if __name__ == "__main__":
    while True:
        try:
            main()
            time.sleep(3)
        except Exception as e:
            print(f"Error occurred: {e}")
            print("Restarting the workflow...")
