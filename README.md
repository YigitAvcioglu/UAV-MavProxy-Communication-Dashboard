This project is a multi-link communication monitoring and control system for UAV ground operations. It allows communication links such as Ubiquiti, RFD900x, 4G/LTE, CUAV P8, and ELRS to be monitored from a single interface by showing their current status, connection quality, and active transmission state. The system supports both manual and automatic selection of the command transmission link, while telemetry can continue to be received through multiple available links for redundancy. It can also forward summarized link information to another Ground Control Station computer in JSON format over UDP. In addition, the project includes basic network diagnostics for checking the reachability and latency of important devices using ping, as well as process management features for controlling MAVProxy-related services. The system can be accessed through both desktop and web interfaces.

# Link Panel
Monitors the status and quality of all communication links in real time and allows manual or automatic selection of the active command transmission link.
<img width="1834" height="969" alt="Screenshot from 2026-09-17 16-37-53" src="https://github.com/user-attachments/assets/13889d31-4fc3-44b7-bd74-345d9d95917f" />

# Network Check Panel
Checks the reachability of configured network devices using ping and displays their connection status, latency, and last check time.
<img width="1848" height="927" alt="Screenshot from 2026-09-17 16-46-38" src="https://github.com/user-attachments/assets/9891c261-fc34-4433-9e1a-21ae26dab059" />

# MavProxy Panel
Provides a centralized interface to configure, start, stop, and monitor MAVProxy, including master inputs, output destinations, and live process logs.
<img width="825" height="910" alt="Screenshot from 2026-09-17 16-38-43" src="https://github.com/user-attachments/assets/2b200a9e-0ef9-4265-8359-a324435a27f8" />

# mavlink-router Panel
Allows the mavlink-router process to be started, stopped, and monitored directly from the dashboard while displaying its live output.
<img width="1654" height="960" alt="Screenshot from 2026-09-17 16-48-24" src="https://github.com/user-attachments/assets/3af8d747-43aa-4468-840f-09d1f5ba5ea6" />
