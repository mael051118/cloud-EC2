from flask import Flask, render_template, request, jsonify, send_file
from io import BytesIO
import pandas as pd
import json
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import os
from functools import lru_cache

app = Flask(__name__)

# Configuración S3
S3_BUCKET = 'second-8a216127-3dd9-4c8b-9191-334301d9f03c-5'
S3_FILE_KEY = 'data_reporte.json'  # Ajusta si está en una subcarpeta ej: 'data/reporte.json'
s3 = boto3.client('s3')

@lru_cache(maxsize=1)  # Cache para evitar descargas repetidas (se actualiza al reiniciar la app)
def load_report_data():
    """Carga y valida el archivo JSON desde S3 con manejo robusto de errores"""
    try:
        # Descargar el archivo desde S3
        response = s3.get_object(Bucket=S3_BUCKET, Key=S3_FILE_KEY)
        file_content = response['Body'].read().decode('utf-8')
        
        # Parsear el JSON
        data = json.loads(file_content)
        
        # Validaciones
        if not isinstance(data, dict):
            raise ValueError("El archivo JSON no contiene un objeto válido")
            
        required_fields = ['total_registros', 'mujeres', 'hombres', 'edad_promedio']
        for field in required_fields:
            if field not in data:
                raise ValueError(f"Campo requerido '{field}' no encontrado en el JSON")
                
        return data
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'NoSuchKey':
            raise FileNotFoundError(f"Archivo '{S3_FILE_KEY}' no encontrado en el bucket S3")
        elif error_code == 'AccessDenied':
            raise PermissionError(f"Sin permisos para acceder al archivo en S3")
        else:
            raise Exception(f"Error de S3 ({error_code}): {str(e)}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Error en formato JSON (posible archivo corrupto): {str(e)}")
    except Exception as e:
        raise Exception(f"Error inesperado al cargar datos: {str(e)}")

@app.route('/')
def index():
    try:
        report_data = load_report_data()
        
        # Preparar datos para gráficos
        stats = {
            "gender": {
                "Female": report_data['mujeres'],
                "Male": report_data['hombres']
            },
            "age_groups": {
                'Menores de 18': report_data['menores_de_18'],
                '18-60': report_data['total_registros'] - report_data['menores_de_18'] - int(report_data['edad_promedio']),
                '60+': int(report_data['edad_promedio'])
            },
            "top_jobs": dict(report_data['top_5_trabajos']),
            "data_quality": {
                "Registros limpios": report_data['registros_limpios'],
                "Registros eliminados": report_data['total_registros'] - report_data['registros_limpios'],
                "Email inválidos": report_data['email_invalidos'],
                "Teléfonos inválidos": report_data['telefono_invalidos']
            }
        }
        
        # Listar archivos en S3 (para mostrar en la interfaz)
        s3_files = []
        try:
            objects = s3.list_objects_v2(Bucket=S3_BUCKET)
            s3_files = [obj['Key'] for obj in objects.get('Contents', [])]
        except ClientError as e:
            print(f"[DEBUG] Error listando archivos S3: {e}")

        return render_template('index.html',
                            stats=stats,
                            report_data=report_data,
                            s3_files=s3_files)
    
    except Exception as e:
        error_msg = f"No se pudieron cargar los datos: {str(e)}"
        print(f"[ERROR] {error_msg}")
        return render_template('error.html', error=error_msg)

@app.route('/export')
def export_data():
    try:
        report_data = load_report_data()
        
        # Crear Excel con múltiples hojas
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            # Hoja de resumen
            summary_df = pd.DataFrame({
                'Métrica': [
                    'Total registros', 'Registros limpios', 'Mujeres', 'Hombres',
                    'Email inválidos', 'Teléfonos inválidos', 'Edad promedio',
                    'Edad mínima', 'Edad máxima', 'Menores de 18', 'Nacidos después del 2000',
                    'Nombres con vocal inicial', 'Registros con todos los campos'
                ],
                'Valor': [
                    report_data['total_registros'], report_data['registros_limpios'],
                    report_data['mujeres'], report_data['hombres'],
                    report_data['email_invalidos'], report_data['telefono_invalidos'],
                    report_data['edad_promedio'], report_data['edad_minima'],
                    report_data['edad_maxima'], report_data['menores_de_18'],
                    report_data['nacidos_despues_2000'], report_data['nombres_con_vocal_inicial'],
                    report_data['registros_con_todos_los_campos']
                ]
            })
            summary_df.to_excel(writer, sheet_name='Resumen', index=False)
            
            # Hoja de nombres repetidos
            pd.DataFrame({
                'Nombres repetidos': report_data['nombres_repetidos']
            }).to_excel(writer, sheet_name='Nombres repetidos', index=False)
            
            # Hoja de trabajos
            pd.DataFrame({
                'Trabajo': [job[0] for job in report_data['top_5_trabajos']],
                'Cantidad': [job[1] for job in report_data['top_5_trabajos']]
            }).to_excel(writer, sheet_name='Top trabajos', index=False)
            
            # Formato profesional
            workbook = writer.book
            header_format = workbook.add_format({
                'bold': True,
                'bg_color': '#4472C4',
                'font_color': 'white',
                'border': 1,
                'align': 'center'
            })
            
            for sheet_name in writer.sheets:
                worksheet = writer.sheets[sheet_name]
                worksheet.autofilter(0, 0, 0, len(writer.sheets[sheet_name].table.columns) - 1)
                worksheet.freeze_panes(1, 0)
                worksheet.set_column(0, 1, 25)
                
                # Aplicar formato a los encabezados
                for col_num, value in enumerate(writer.sheets[sheet_name].table.columns):
                    worksheet.write(0, col_num, value, header_format)
        
        output.seek(0)
        
        # Subir a S3 (opcional)
        filename = f"reporte_exportado_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        try:
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=f"exports/{filename}",
                Body=output.getvalue(),
                ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
        except ClientError as e:
            print(f"[WARNING] No se pudo guardar en S3: {e}")

        # Descargar el archivo
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        error_msg = f"Error al exportar: {str(e)}"
        print(f"[ERROR] {error_msg}")
        return render_template('error.html', error=error_msg)

@app.route('/debug')
def debug():
    """Ruta para diagnóstico de problemas"""
    try:
        debug_info = {
            "s3_bucket": S3_BUCKET,
            "s3_file_key": S3_FILE_KEY,
            "bucket_access": "OK" if s3.list_objects_v2(Bucket=S3_BUCKET, MaxKeys=1) else "Error",
            "file_exists": False,
            "file_accessible": False
        }
        
        try:
            s3.head_object(Bucket=S3_BUCKET, Key=S3_FILE_KEY)
            debug_info.update({
                "file_exists": True,
                "file_accessible": True
            })
        except ClientError as e:
            debug_info["s3_error"] = str(e)
        
        return jsonify(debug_info)
    
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)