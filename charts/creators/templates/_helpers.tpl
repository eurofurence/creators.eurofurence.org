{{- define "creators.name" -}}
{{- printf "%s-creators" .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- define "creators.labels" -}}
app.kubernetes.io/name: creators
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "creators.container" -}}
image: {{ printf "%s:%s" (required "image.repository is required" .Values.image.repository) (required "image.tag is required" .Values.image.tag) | quote }}
imagePullPolicy: {{ .Values.image.pullPolicy }}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: [ALL]
env:
{{- range $key, $value := .Values.env }}
  - name: {{ $key }}
    value: {{ $value | quote }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .Values.envFrom }}
envFrom:
{{ toYaml . | indent 2 }}
{{- end }}
resources:
{{ toYaml .Values.resources | indent 2 }}
volumeMounts:
  - name: scratch
    mountPath: /tmp
{{- end -}}
{{- define "creators.pod" -}}
automountServiceAccountToken: false
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
  seccompProfile:
    type: RuntimeDefault
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
{{ toYaml . | indent 2 }}
{{- end }}
volumes:
  - name: scratch
    emptyDir:
      sizeLimit: 128Mi
{{- end -}}
