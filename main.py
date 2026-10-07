import yaml
import json
import requests
import asyncio
import aiohttp

# ==========================================
# CLASES DE DOMINIO
# ==========================================

class Student:
    def __init__(self, name, mac):
        self.name = name
        self.mac = mac.upper()

    def __repr__(self):
        return f"Student(Name: {self.name}, MAC: {self.mac})"


class Service:
    def __init__(self, name, protocol, port):
        self.name = name
        self.protocol = protocol
        self.port = port

    def __repr__(self):
        return f"Service({self.name}, {self.protocol}:{self.port})"


class Server:
    def __init__(self, name, ip, services):
        self.name = name
        self.ip = ip
        self.services = []
        for s in services:
            if isinstance(s, dict):
                self.services.append(Service(s["name"], s["protocol"], s["port"]))
            else:
                self.services.append(s)

    def get_service(self, service_name):
        for s in self.services:
            if s.name == service_name:
                return s
        return None

    def __repr__(self):
        return f"Server(Name: {self.name}, IP: {self.ip})"


class Course:
    def __init__(self, name, code, state, students, servers):
        self.name = name
        self.code = code
        self.state = state
        self.students = students
        self.servers = servers

    def __repr__(self):
        return f"Course({self.code} - {self.name}, State: {self.state})"


# ==========================================
# CARGA Y GUARDADO DE DATOS
# ==========================================

def load_data(filename):
    with open(filename, "r") as file:
        data = yaml.safe_load(file)
    
    students = [Student(x["name"], x["mac"]) for x in data.get("students", [])]
    servers = [Server(y["name"], y["ip"], y["services"]) for y in data.get("servers", [])]
    courses = [
        Course(z["name"], z.get("code", ""), z["state"], z.get("students", []), z.get("servers", []))
        for z in data.get("courses", [])
    ]
    return students, servers, courses


def save_data(filename, students, servers, courses):
    data = {
        "students": [{"name": s.name, "mac": s.mac} for s in students],
        "servers": [
            {
                "name": s.name,
                "ip": s.ip,
                "services": [
                    {"name": serv.name, "protocol": serv.protocol, "port": serv.port}
                    for serv in s.services
                ],
            }
            for s in servers
        ],
        "courses": [
            {
                "name": c.name,
                "code": c.code,
                "state": c.state,
                "students": c.students,
                "servers": c.servers,
            }
            for c in courses
        ],
    }
    with open(filename, "w") as file:
        yaml.safe_dump(data, file, default_flow_style=False)


# ==========================================
# FUNCIONES AUXILIARES REST / FLOODLIGHT
# ==========================================

def get_attachment_point(controller_ip, mac):
    endpoint = '/wm/device/'
    url = f'http://{controller_ip}:8080{endpoint}'
    try:
        response = requests.get(url, params={'mac': mac}, timeout=5)
        response.raise_for_status()
        results = response.json()
        if not results:
            return None, None
        
        result = results[0]
        attachment_points = result.get("attachmentPoint", [])
        if not attachment_points:
            return None, None

        attachment_point = attachment_points[0]
        return attachment_point["switchDPID"], attachment_point["port"]
    except Exception as e:
        print(f"Error consultando punto de conexión para {mac}: {e}")
        return None, None


def get_route(controller_ip, src_mac, dst_mac):
    src_sw_dpid, src_sw_port = get_attachment_point(controller_ip, src_mac)
    dst_sw_dpid, dst_sw_port = get_attachment_point(controller_ip, dst_mac)
    
    if not src_sw_dpid or not dst_sw_dpid:
        print(f"No se pudo determinar el punto de conexión para {src_mac} o {dst_mac}")
        return None

    endpoint = f'/wm/topology/route/{src_sw_dpid}/{src_sw_port}/{dst_sw_dpid}/{dst_sw_port}/json'
    url = f'http://{controller_ip}:8080{endpoint}'
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error obteniendo ruta: {e}")
        return None


# ==========================================
# GESTIÓN ASÍNCRONA DE FLOWS (BUILD_ROUTE)
# ==========================================

async def push_flow_entry(session, controller_ip, flow_data):
    url = f"http://{controller_ip}:8080/wm/staticflowpusher/json"
    try:
        async with session.post(url, json=flow_data) as resp:
            return await resp.text()
    except Exception as e:
        return f"Error insertando flujo: {e}"


async def delete_flow_entry(session, controller_ip, flow_name):
    url = f"http://{controller_ip}:8080/wm/staticflowpusher/json"
    try:
        async with session.delete(url, json={"name": flow_name}) as resp:
            return await resp.text()
    except Exception as e:
        return f"Error eliminando flujo: {e}"


def build_route(controller_ip, route_data, student_mac, server_ip, service_proto, service_port, handler):
    if not route_data or not isinstance(route_data, list):
        print("Estructura de ruta no válida.")
        return []

    flows = []
    ip_proto = "6" if service_proto.upper() == "TCP" else "17"

    for i in range(0, len(route_data), 2):
        node_in = route_data[i]
        node_out = route_data[i+1] if i+1 < len(route_data) else None
        
        dpid = node_in["switch"]
        in_port = str(node_in["port"]["portNumber"])
        out_port = str(node_out["port"]["portNumber"]) if node_out else None

        if not out_port:
            continue

        flows.append({
            "switch": dpid,
            "name": f"{handler}_fw_{i//2}",
            "cookie": "0",
            "priority": "30000",
            "in_port": in_port,
            "eth_type": "0x0800",
            "eth_src": student_mac.lower(),
            "ipv4_dst": server_ip,
            "ip_proto": ip_proto,
            "tp_dst": str(service_port),
            "active": "true",
            "actions": f"output={out_port}"
        })

        flows.append({
            "switch": dpid,
            "name": f"{handler}_rv_{i//2}",
            "cookie": "0",
            "priority": "30000",
            "in_port": out_port,
            "eth_type": "0x0800",
            "ipv4_src": server_ip,
            "eth_dst": student_mac.lower(),
            "ip_proto": ip_proto,
            "tp_src": str(service_port),
            "active": "true",
            "actions": f"output={in_port}"
        })

        flows.append({
            "switch": dpid,
            "name": f"{handler}_arp_fw_{i//2}",
            "cookie": "0",
            "priority": "20000",
            "in_port": in_port,
            "eth_type": "0x0806",
            "active": "true",
            "actions": f"output={out_port}"
        })

        flows.append({
            "switch": dpid,
            "name": f"{handler}_arp_rv_{i//2}",
            "cookie": "0",
            "priority": "20000",
            "in_port": out_port,
            "eth_type": "0x0806",
            "active": "true",
            "actions": f"output={in_port}"
        })

    async def install_all():
        async with aiohttp.ClientSession() as session:
            tasks = [push_flow_entry(session, controller_ip, flow) for flow in flows]
            await asyncio.gather(*tasks)

    asyncio.run(install_all())
    return [f["name"] for f in flows]


def remove_route_flows(controller_ip, flow_names):
    async def delete_all():
        async with aiohttp.ClientSession() as session:
            tasks = [delete_flow_entry(session, controller_ip, name) for name in flow_names]
            await asyncio.gather(*tasks)

    asyncio.run(delete_all())


# ==========================================
# LÓGICA DE NEGOCIO / VALIDACIONES
# ==========================================

def is_student_authorized(student_name, server_name, service_name, courses):
    for course in courses:
        if course.state.upper() == "ACTIVE" and student_name in course.students:
            for srv in course.servers:
                if srv["name"] == server_name and service_name in srv.get("allowed_services", []):
                    return True
    return False


connections_db = {}

# ==========================================
# INTERFAZ CLI Y MENÚS
# ==========================================

def menu():
    print("\n" + "#" * 45)
    print("Network Policy Manager de la UPSM")
    print("#" * 45)
    print("\nSeleccione una opción:\n")
    print("1) Importar")
    print("2) Exportar")
    print("3) Cursos")
    print("4) Alumnos")
    print("5) Servidores")
    print("6) Políticas")
    print("7) Conexiones")
    print("8) Salir")


def main():
    controller_ip = "10.100.18.253"
    db_filename = "database.yaml"
    students, servers, courses = load_data(db_filename)

    while True:
        menu()
        choice = input(">>> ").strip()

        if choice == "1":
            filename = input("Ingrese el nombre del archivo a importar [database.yaml]: ").strip() or "database.yaml"
            try:
                students, servers, courses = load_data(filename)
                print(f"[+] Datos importados exitosamente desde '{filename}'.")
            except Exception as e:
                print(f"[-] Error al importar el archivo: {e}")

        elif choice == "2":
            filename = input("Ingrese el nombre del archivo para guardar [database.yaml]: ").strip() or "database.yaml"
            try:
                save_data(filename, students, servers, courses)
                print(f"[+] Datos exportados exitosamente a '{filename}'.")
            except Exception as e:
                print(f"[-] Error al exportar los datos: {e}")

        elif choice == "3":
            print("\n--- MENÚ CURSOS ---")
            print("1) Listar (*)")
            print("2) Mostrar detalles (*)")
            print("3) Gestionar alumnos (*)")
            print("4) Volver")
            sub = input(">>> ").strip()

            if sub == "1":
                print("\n--- Lista de Cursos ---")
                for c in courses:
                    print(f"Código: {c.code} | Nombre: {c.name} | Estado: {c.state}")

            elif sub == "2":
                c_code = input("Ingrese el código del curso (ej. HGW101): ").strip()
                course = next((c for c in courses if c.code.upper() == c_code.upper()), None)
                if course:
                    print(f"\nCurso: {course.name} ({course.code}) - Estado: {course.state}")
                    print("Alumnos inscritos:", ", ".join(course.students) if course.students else "Ninguno")
                    print("Servidores y Servicios autorizados:")
                    for srv in course.servers:
                        print(f"  - Servidor: {srv['name']} | Servicios: {', '.join(srv['allowed_services'])}")
                else:
                    print("[-] Curso no encontrado.")

            elif sub == "3":
                c_code = input("Ingrese el código del curso a modificar: ").strip()
                course = next((c for c in courses if c.code.upper() == c_code.upper()), None)
                if course:
                    print("1) Agregar alumno")
                    print("2) Eliminar alumno")
                    opt = input(">>> ").strip()
                    s_name = input("Nombre del alumno: ").strip()
                    if opt == "1":
                        if s_name not in course.students:
                            course.students.append(s_name)
                            print(f"[+] Alumno {s_name} agregado a {course.code}.")
                        else:
                            print("[-] El alumno ya se encuentra matriculado.")
                    elif opt == "2":
                        if s_name in course.students:
                            course.students.remove(s_name)
                            print(f"[+] Alumno {s_name} eliminado de {course.code}.")
                        else:
                            print("[-] El alumno no pertenece a este curso.")

        elif choice == "4":
            print("\n--- MENÚ ALUMNOS ---")
            print("1) Crear (*)")
            print("2) Listar (*)")
            print("3) Mostrar detalles (*)")
            print("4) Volver")
            sub = input(">>> ").strip()

            if sub == "1":
                name = input("Nombre del nuevo alumno: ").strip()
                mac = input("Dirección MAC (ej. 00:44:11:22:44:A7): ").strip()
                students.append(Student(name, mac))
                print(f"[+] Alumno '{name}' registrado exitosamente.")

            elif sub == "2":
                filter_course = input("Filtrar por código de curso (presione Enter para listar todos): ").strip()
                print("\n--- Lista de Alumnos ---")
                if filter_course:
                    course = next((c for c in courses if c.code.upper() == filter_course.upper()), None)
                    if course:
                        filtered_students = [s for s in students if s.name in course.students]
                        for s in filtered_students:
                            print(f"- {s.name} (MAC: {s.mac})")
                    else:
                        print("[-] Curso no encontrado.")
                else:
                    for s in students:
                        print(f"- {s.name} (MAC: {s.mac})")

            elif sub == "3":
                s_name = input("Nombre del alumno: ").strip()
                st = next((s for s in students if s.name.lower() == s_name.lower()), None)
                if st:
                    print(f"\nDetalles del Alumno:")
                    print(f"Nombre: {st.name}")
                    print(f"MAC: {st.mac}")
                    user_courses = [c.code for c in courses if st.name in c.students]
                    print(f"Cursos matriculados: {', '.join(user_courses) if user_courses else 'Ninguno'}")
                else:
                    print("[-] Alumno no encontrado.")

        elif choice == "5":
            print("\n--- MENÚ SERVIDORES Y SERVICIOS ---")
            print("1) Listar (*)")
            print("2) Mostrar servicios (*)")
            print("3) Volver")
            sub = input(">>> ").strip()

            if sub == "1":
                print("\n--- Servidores Existentes ---")
                for srv in servers:
                    print(f"- Nombre: {srv.name} | IP: {srv.ip}")

            elif sub == "2":
                srv_name = input("Nombre del servidor (ej. Server 3): ").strip()
                srv = next((s for s in servers if s.name.lower() == srv_name.lower()), None)
                if srv:
                    print(f"\nServicios de {srv.name} ({srv.ip}):")
                    for serv in srv.services:
                        print(f"  - Servicio: {serv.name} | Protocolo: {serv.protocol} | Puerto: {serv.port}")
                else:
                    print("[-] Servidor no encontrado.")

        elif choice == "6":
            print("\n--- CONSULTA DE POLÍTICAS ---")
            srv_name = input("Nombre del servidor: ").strip()
            serv_name = input("Nombre del servicio: ").strip()
            print(f"\nCursos con acceso a '{serv_name}' en '{srv_name}':")
            found = False
            for c in courses:
                for s in c.servers:
                    if s["name"].lower() == srv_name.lower() and serv_name in s.get("allowed_services", []):
                        print(f"- {c.code} ({c.name}) [Estado: {c.state}]")
                        found = True
            if not found:
                print("[-] Ningún curso tiene asignado ese servicio en dicho servidor.")

        elif choice == "7":
            print("\n--- MENÚ CONEXIONES ---")
            print("1) Crear (*)")
            print("2) Listar (*)")
            print("3) Borrar (*)")
            print("4) Volver")
            sub = input(">>> ").strip()

            if sub == "1":
                student_name = input("Nombre del alumno: ").strip()
                server_name = input("Nombre del servidor destino: ").strip()
                service_name = input("Nombre del servicio (ej. ssh, web): ").strip()
                handler = input("Ingrese un identificador (handler) para esta conexión: ").strip()

                student = next((s for s in students if s.name.lower() == student_name.lower()), None)
                server = next((s for s in servers if s.name.lower() == server_name.lower()), None)

                if not student or not server:
                    print("[-] Alumno o servidor no encontrado.")
                    continue

                service = server.get_service(service_name)
                if not service:
                    print(f"[-] El servicio '{service_name}' no existe en el {server.name}.")
                    continue

                if not is_student_authorized(student.name, server.name, service.name, courses):
                    print(f"\n[ERROR] ACCESO DENEGADO: El alumno '{student.name}' NO está autorizado para acceder al servicio '{service.name}' en '{server.name}'.")
                    continue

                print(f"[+] Alumno autorizado. Calculando ruta de red...")

                route_data = get_route(controller_ip, student.mac, "00:00:00:00:00:03")

                if not route_data:
                    print("[-] No se pudo calcular la ruta desde el controlador SDN.")
                    continue

                flow_names = build_route(
                    controller_ip,
                    route_data,
                    student.mac,
                    server.ip,
                    service.protocol,
                    service.port,
                    handler
                )

                connections_db[handler] = {
                    "student": student.name,
                    "server": server.name,
                    "service": service.name,
                    "flows": flow_names
                }
                print(f"[+] Conexión '{handler}' establecida exitosamente mediante reprogramación de flujos.")

            elif sub == "2":
                print("\n--- Conexiones Activas Creadas Manualmente ---")
                if not connections_db:
                    print("No hay conexiones activas.")
                else:
                    for h, data in connections_db.items():
                        print(f"Handler: {h} | Alumno: {data['student']} -> Servidor: {data['server']} (Servicio: {data['service']})")

            elif sub == "3":
                handler = input("Ingrese el handler de la conexión a borrar: ").strip()
                if handler in connections_db:
                    conn = connections_db[handler]
                    remove_route_flows(controller_ip, conn["flows"])
                    del connections_db[handler]
                    print(f"[+] Conexión '{handler}' y sus flujos asociados eliminados de los switches.")
                else:
                    print("[-] Handler de conexión no encontrado.")

        elif choice == "8":
            print("Saliendo de la aplicación...")
            break


if __name__ == "__main__":
    main()