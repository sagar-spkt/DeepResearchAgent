# Kubernetes application chart

Render/lint with immutable backend/web digests. AWS requires vllm.enabled=true plus pinned image and model revision, private service key and application secret. Create namespace research and install the prerequisites listed in infra/README.md first. Default ingress is disabled. TLS ingress never exposes Langfuse or vLLM.

The application secret contains DATABASE_URL, queue/document configuration, authentication settings, VLLM_API_KEY and tracing credentials. Never put secret values into committed values files. Populate Kubernetes Secrets from Secrets Manager using an authorized bootstrap job or a separately installed reviewed secret controller. Web must not receive backend secrets.

One API/worker/web replica is intentional demo capacity. The backend must enforce one globally active run. Pod Identity is attached to research-application in namespace research. The chart does not create Langfuse's privileged gateway or its storage IAM. Deploy those only after backend administrator authorization is implemented and tested. Local Compose provides an observability stack, not production durability.

The GPU image, CUDA compatibility, Qwen tool parser, context/cache behavior, actual CPU/memory requests and GPU device plugin require staging validation. No zero-downtime or HA claim. The model NetworkPolicy requires EKS VPC CNI network-policy support; test denied access from unrelated pods before deployment acceptance.
