import requests
from requests.auth import HTTPDigestAuth
from datetime import datetime

class DahuaCamera:
    def __init__(self, ip, username, password):
        self.ip = ip
        self.username = username
        self.password = password
        self.base_url = f"http://{ip}/cgi-bin"
        self.session = requests.Session()
  
    def get_current_time(self):
        """
        Получить текущее время камеры
        
        Returns:
            dict: Словарь с результатами:
                - success (bool): Успешность операции
                - camera_time (str): Время камеры в формате "yyyy-mm-dd hh:mm:ss"
                - raw_response (str): Сырой ответ от камеры
                - local_time (str): Текущее локальное время для сравнения
        """
        url = f"{self.base_url}/global.cgi?action=getCurrentTime"
        
        try:
            response = self.session.get(
                url,
                auth=HTTPDigestAuth(self.username, self.password),
                timeout=5
            )
            
            if response.status_code == 200:
                # Dahua обычно возвращает время в формате: "2024-02-05 14:30:45"
                camera_time = response.text.strip()
                
                # Валидация формата времени
                try:
                    parsed_time = datetime.strptime(camera_time, "%Y-%m-%d %H:%M:%S")
                    is_valid = True
                except ValueError:
                    is_valid = False
                
                result = {
                    "success": True,
                    "camera_time": camera_time,
                    "raw_response": response.text,
                    "local_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "is_valid_format": is_valid
                }
                
                if is_valid:
                    print(f"✓ Текущее время камеры: {camera_time}")
                else:
                    print(f"⚠ Получено время в нестандартном формате: {camera_time}")
                
                return result
            else:
                print(f"✗ Ошибка получения времени: {response.status_code} - {response.text}")
                return {
                    "success": False,
                    "error_code": response.status_code,
                    "error_message": response.text.strip()
                }
                
        except requests.exceptions.Timeout:
            print("✗ Таймаут подключения к камере")
            return {"success": False, "error": "timeout"}
        except requests.exceptions.ConnectionError:
            print("✗ Ошибка подключения к камере")
            return {"success": False, "error": "connection_error"}
        except Exception as e:
            print(f"✗ Неизвестная ошибка: {e}")
            return {"success": False, "error": str(e)}
        
    def open_strobe(self, channel=1, plate_number="", open_type="Normal"):
        '''
        Открыть стробоскоп/ворота через Traffic Snap API
        
        Args:
            channel: канал
            plate_number: номер распознанного авто
            open_type: "Normal", "Emergency" и т.д.
        '''
        url = f"{self.base_url}/trafficSnap.cgi"
        params = {
            "action": "openStrobe",
            "channel": channel,
            "info.openType": open_type,
            "info.plateNumber": plate_number
        }
        
        try:
            response = self.session.get(
                url,
                params=params,
                auth=HTTPDigestAuth(self.username, self.password),
                timeout=5
            )
            
            if response.status_code == 200 and response.text.strip() == "OK":
                print(f"✓ Ворота открыты для номера {plate_number}")
                return True
            else:
                print(f"✗ Ошибка: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            print(f"✗ Ошибка: {e}")
            return False
        
# Пример использования
if __name__ == "__main__":
    # Настройки камеры
    CAMERA_IP = "192.168.84.2"
    USERNAME = "admin"
    PASSWORD = "Donsmart2019"
    
    camera = DahuaCamera(CAMERA_IP, USERNAME, PASSWORD)

    # Получение текущего времени камеры
    print("=== Получение времени камеры ===")
    result = camera.get_current_time()
    
    if result["success"]:
        print(f"Время камеры: {result['camera_time']}")
        print(f"Локальное время: {result['local_time']}")
        
        # Расчет разницы во времени (опционально)
        try:
            cam_dt = datetime.strptime(result['camera_time'], "%Y-%m-%d %H:%M:%S")
            local_dt = datetime.strptime(result['local_time'], "%Y-%m-%d %H:%M:%S")
            diff = abs((cam_dt - local_dt).total_seconds())
            print(f"Разница: {diff:.0f} секунд")
            
            if diff > 60:
                print("⚠ Время камеры расходится с локальным более чем на 1 минуту!")
        except:
            pass
    
    # Пробуем открыть ворота через trafficSnap (для ANPR)
    
    print("\n=== Попытка открыть ворота ===")
    if camera.open_strobe(channel=1, plate_number="A055AA77", open_type="Normal"):
        print("Успех!")  