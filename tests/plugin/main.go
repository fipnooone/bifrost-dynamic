// SPDX-License-Identifier: Apache-2.0
// CI-only probe. This is not bundled into the published runtime image.
package main

import (
	"fmt"

	"github.com/maximhq/bifrost/core/schemas"
)

const marker = "native-plugin-executed"

func GetName() string { return "dynamic-image-smoke" }

func Init(config any) error {
	m, ok := config.(map[string]any)
	if !ok || m["marker"] != marker {
		return fmt.Errorf("smoke plugin did not receive its configuration")
	}
	fmt.Println("dynamic-image-smoke: initialized")
	return nil
}

func Cleanup() error { return nil }

func PreLLMHook(_ *schemas.BifrostContext, req *schemas.BifrostRequest) (*schemas.BifrostRequest, *schemas.LLMPluginShortCircuit, error) {
	return req, nil, nil
}

func PostLLMHook(_ *schemas.BifrostContext, resp *schemas.BifrostResponse, bifrostErr *schemas.BifrostError) (*schemas.BifrostResponse, *schemas.BifrostError, error) {
	if resp == nil || resp.ListModelsResponse == nil {
		return resp, bifrostErr, nil
	}
	result := *resp
	list := *resp.ListModelsResponse
	list.Data = append([]schemas.Model(nil), list.Data...)
	for i := range list.Data {
		if list.Data[i].ID == "smoke/dynamic-probe" {
			name, contextLength := marker, 123456
			list.Data[i].Name = &name
			list.Data[i].ContextLength = &contextLength
		}
	}
	result.ListModelsResponse = &list
	return &result, bifrostErr, nil
}

func HTTPTransportPostHook(_ *schemas.BifrostContext, req *schemas.HTTPRequest, resp *schemas.HTTPResponse) error {
	if req != nil && resp != nil && req.Method == "GET" && req.Path == "/v1/models" {
		if resp.Headers == nil {
			resp.Headers = make(map[string]string)
		}
		resp.Headers["X-Bifrost-Dynamic-Smoke"] = marker
	}
	return nil
}
